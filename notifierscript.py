from __future__ import annotations

import logging
import math
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv


DETAIL_URL_TEMPLATE = (
	"https://registration.banner.gatech.edu/StudentRegistrationSsb/ssb/"
	"searchResults/getEnrollmentInfo?term={term}&courseReferenceNumber={crn}"
)
REGISTRATION_MENU_URL = "https://registration.banner.gatech.edu/StudentRegistrationSsb/ssb/term/termSelection?mode=registration"
CLASS_SEARCH_BOOTSTRAP_URL = "https://registration.banner.gatech.edu/StudentRegistrationSsb/ssb/term/termSelection?mode=search"
DEFAULT_USER_AGENT = (
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
	"AppleWebKit/537.36 (KHTML, like Gecko) "
	"Chrome/127.0.0.0 Safari/537.36"
)


class ParseError(Exception):
	pass


class TemporaryBlockedError(Exception):
	def __init__(self, status_code: int, retry_after: float = 0, service: str = "OSCAR"):
		super().__init__(f"{service} returned HTTP {status_code}.")
		self.status_code = status_code
		self.retry_after = retry_after
		self.service = service


class WebhookError(Exception):
	"""A delivery error that does not expose the secret webhook URL in logs."""
	def __init__(self, message: str, retryable: bool = True):
		super().__init__(message)
		self.retryable = retryable


def _retry_after_seconds(response: requests.Response) -> float:
	value = response.headers.get("Retry-After", "")
	try:
		delay = float(value)
		return max(0, delay) if math.isfinite(delay) else 0
	except ValueError:
		try:
			when = parsedate_to_datetime(value)
			return max(0, (when - datetime.now(timezone.utc)).total_seconds())
		except (TypeError, ValueError, OverflowError):
			return 0


@dataclass
class RowAvailability:
	capacity: int
	actual: int
	remaining: int


@dataclass
class Availability:
	seats: RowAvailability
	waitlist: RowAvailability


@dataclass
class Config:
	webhook_url: str
	webhook_type: str
	term_id: str
	target_crns: list[str]
	poll_min_seconds: int
	poll_max_seconds: int
	request_timeout_seconds: int
	blocked_backoff_min_seconds: int
	blocked_backoff_max_seconds: int
	user_agent: str
	run_once: bool
	dry_run: bool

	@staticmethod
	def from_env() -> "Config":
		load_dotenv(Path(__file__).resolve().parent / ".env")

		webhook_url = _require_env("WEBHOOK_URL")
		term_id = _require_env("TERM_ID")
		target_crns = [part.strip() for part in _require_env("TARGET_CRNS").split(",") if part.strip()]
		if not target_crns:
			raise ValueError("TARGET_CRNS cannot be empty.")

		webhook_type = os.getenv("WEBHOOK_TYPE", "auto").strip().lower()
		poll_min_seconds = int(os.getenv("POLL_MIN_SECONDS", "30"))
		poll_max_seconds = int(os.getenv("POLL_MAX_SECONDS", "45"))
		request_timeout_seconds = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
		blocked_backoff_min_seconds = int(os.getenv("BLOCKED_BACKOFF_MIN_SECONDS", "300"))
		blocked_backoff_max_seconds = int(os.getenv("BLOCKED_BACKOFF_MAX_SECONDS", "900"))
		user_agent = os.getenv("USER_AGENT", DEFAULT_USER_AGENT)
		run_once = _to_bool(os.getenv("RUN_ONCE", "false"))
		dry_run = _to_bool(os.getenv("DRY_RUN", "false"))

		if poll_min_seconds <= 0 or poll_max_seconds <= 0:
			raise ValueError("Polling interval values must be positive.")
		if poll_min_seconds > poll_max_seconds:
			raise ValueError("POLL_MIN_SECONDS cannot be greater than POLL_MAX_SECONDS.")
		if blocked_backoff_min_seconds > blocked_backoff_max_seconds:
			raise ValueError(
				"BLOCKED_BACKOFF_MIN_SECONDS cannot be greater than BLOCKED_BACKOFF_MAX_SECONDS."
			)

		return Config(
			webhook_url=webhook_url,
			webhook_type=webhook_type,
			term_id=term_id,
			target_crns=target_crns,
			poll_min_seconds=poll_min_seconds,
			poll_max_seconds=poll_max_seconds,
			request_timeout_seconds=request_timeout_seconds,
			blocked_backoff_min_seconds=blocked_backoff_min_seconds,
			blocked_backoff_max_seconds=blocked_backoff_max_seconds,
			user_agent=user_agent,
			run_once=run_once,
			dry_run=dry_run,
		)


def _require_env(name: str) -> str:
	value = os.getenv(name)
	if value is None or not value.strip():
		raise ValueError(f"Missing required environment variable: {name}")
	return value.strip()


def _to_int(value: str) -> Optional[int]:
	match = re.search(r"-?\d+", value.replace(",", ""))
	if not match:
		return None
	return int(match.group(0))


def _to_bool(value: str) -> bool:
	return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _extract_row(row: Tag, header_map: dict[str, int]) -> Optional[RowAvailability]:
	cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
	if len(cells) < 2:
		return None

	def from_index(index: Optional[int]) -> Optional[int]:
		if index is None or index >= len(cells):
			return None
		return _to_int(cells[index])

	cap = from_index(header_map.get("capacity"))
	act = from_index(header_map.get("actual"))
	rem = from_index(header_map.get("remaining"))

	if cap is not None and act is not None and rem is not None:
		return RowAvailability(capacity=cap, actual=act, remaining=rem)

	fallback_numbers = [_to_int(cell) for cell in cells[1:]]
	fallback_numbers = [value for value in fallback_numbers if value is not None]
	if len(fallback_numbers) >= 3:
		return RowAvailability(
			capacity=fallback_numbers[0],
			actual=fallback_numbers[1],
			remaining=fallback_numbers[2],
		)
	if len(fallback_numbers) == 1:
		return RowAvailability(capacity=0, actual=0, remaining=fallback_numbers[0])

	return None


def parse_registration_availability(html: str) -> Availability:
	soup = BeautifulSoup(html, "html.parser")
	tables = soup.find_all("table")

	candidate_tables: list[Tag] = []
	for table in tables:
		text = table.get_text(" ", strip=True).lower()
		if "registration availability" in text or (
			"waitlist seats" in text and "remaining" in text and "capacity" in text
		):
			candidate_tables.append(table)

	if not candidate_tables:
		candidate_tables = [table for table in tables]

	for table in candidate_tables:
		header_row = table.find("tr")
		header_map: dict[str, int] = {}
		if header_row:
			header_cells = [cell.get_text(" ", strip=True).lower() for cell in header_row.find_all(["th", "td"])]
			for idx, label in enumerate(header_cells):
				if "capacity" in label:
					header_map["capacity"] = idx
				elif label == "actual" or "actual" in label:
					header_map["actual"] = idx
				elif "remaining" in label:
					header_map["remaining"] = idx

		seat_row: Optional[RowAvailability] = None
		waitlist_row: Optional[RowAvailability] = None

		for row in table.find_all("tr"):
			row_text = row.get_text(" ", strip=True).lower()
			if "seats" in row_text and "waitlist" not in row_text:
				parsed = _extract_row(row, header_map)
				if parsed:
					seat_row = parsed
			elif "waitlist seats" in row_text:
				parsed = _extract_row(row, header_map)
				if parsed:
					waitlist_row = parsed

		if seat_row and waitlist_row:
			return Availability(seats=seat_row, waitlist=waitlist_row)

	raise ParseError("Could not parse registration availability from OSCAR HTML response.")


def parse_enrollment_info(html: str) -> Availability:
	text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

	def extract(pattern: str, label: str) -> int:
		match = re.search(pattern, text, flags=re.IGNORECASE)
		if not match:
			raise ParseError(f"Missing '{label}' in enrollment response.")
		return int(match.group(1).replace(",", ""))

	seats_actual = extract(r"Enrollment\s+Actual:\s*(-?[\d,]+)", "Enrollment Actual")
	seats_capacity = extract(r"Enrollment\s+Maximum:\s*(-?[\d,]+)", "Enrollment Maximum")
	seats_remaining = extract(r"Enrollment\s+Seats\s+Available:\s*(-?[\d,]+)", "Enrollment Seats Available")
	wait_capacity = extract(r"Waitlist\s+Capacity:\s*(-?[\d,]+)", "Waitlist Capacity")
	wait_actual = extract(r"Waitlist\s+Actual:\s*(-?[\d,]+)", "Waitlist Actual")
	wait_remaining = extract(r"Waitlist\s+Seats\s+Available:\s*(-?[\d,]+)", "Waitlist Seats Available")

	return Availability(
		seats=RowAvailability(capacity=seats_capacity, actual=seats_actual, remaining=seats_remaining),
		waitlist=RowAvailability(capacity=wait_capacity, actual=wait_actual, remaining=wait_remaining),
	)


def _check_oscar_response(response: requests.Response) -> None:
	if response.status_code in (403, 429, 503):
		raise TemporaryBlockedError(response.status_code, _retry_after_seconds(response))
	response.raise_for_status()
	if urlsplit(response.url).hostname != "registration.banner.gatech.edu":
		raise ParseError("OSCAR redirected away from public class search; data is unavailable.")


def fetch_availability(session: requests.Session, config: Config, crn: str) -> Availability:
	# Prime guest session cookies for Banner SSB APIs.
	bootstrap = session.get(
		CLASS_SEARCH_BOOTSTRAP_URL,
		headers={"User-Agent": config.user_agent},
		timeout=config.request_timeout_seconds,
	)
	_check_oscar_response(bootstrap)

	url = DETAIL_URL_TEMPLATE.format(term=config.term_id, crn=crn)
	response = session.get(
		url,
		headers={"User-Agent": config.user_agent},
		timeout=config.request_timeout_seconds,
	)

	_check_oscar_response(response)

	try:
		return parse_enrollment_info(response.text)
	except ParseError as exc:
		raise ParseError(
			f"Failed to parse enrollment data for CRN {crn}. "
			"Banner may be in maintenance mode or the term/CRN may not be accessible."
		) from exc


def _resolve_webhook_type(config: Config) -> str:
	if config.webhook_type != "auto":
		return config.webhook_type
	parts = urlsplit(config.webhook_url)
	if parts.hostname in {"discord.com", "discordapp.com"} and re.match(r"^/api(?:/v\d+)?/webhooks/", parts.path):
		return "discord"
	if parts.hostname == "api.telegram.org":
		return "telegram"
	return "generic"


def send_alert(config: Config, crn: str, alert_type: str, availability: Availability) -> None:
	webhook_type = _resolve_webhook_type(config)
	detail_url = "https://registration.banner.gatech.edu/StudentRegistrationSsb/ssb/term/termSelection?mode=search"

	if alert_type == "seat_open":
		title = "Seat Open"
		body = f"Regular seats available: {availability.seats.remaining}"
	elif alert_type == "waitlist_lockout":
		title = "Seat Open But Waitlist Lockout"
		body = (
			f"Regular seats show {availability.seats.remaining}, but waitlist has "
			f"{availability.waitlist.actual} student(s). Add/drop may prioritize them first."
		)
	elif alert_type == "waitlist_open":
		title = "Waitlist Spot Open"
		body = f"Waitlist spots available: {availability.waitlist.remaining}"
	else:
		title = "Course Availability Update"
		body = "Course state changed."

	message = (
		f"[{title}] GT CRN {crn} (Term {config.term_id})\n"
		f"{body}\n"
		f"Seats: cap={availability.seats.capacity}, actual={availability.seats.actual}, rem={availability.seats.remaining}\n"
		f"Waitlist: cap={availability.waitlist.capacity}, actual={availability.waitlist.actual}, rem={availability.waitlist.remaining}\n"
		f"Detail: {detail_url}\n"
		f"Add/Drop: {REGISTRATION_MENU_URL}"
	)

	if webhook_type == "discord":
		payload = {"content": message, "allowed_mentions": {"parse": []}}
	elif webhook_type == "telegram":
		payload = {"text": message, "disable_web_page_preview": True}
	else:
		payload = {
			"text": message,
			"crn": crn,
			"term": config.term_id,
			"type": alert_type,
			"seats_remaining": availability.seats.remaining,
			"waitlist_remaining": availability.waitlist.remaining,
		}

	if config.dry_run:
		logging.info("DRY_RUN enabled. Skipping webhook post. Payload=%s", payload)
		return

	url = config.webhook_url
	if webhook_type == "discord":
		# Discord's default wait=false does not confirm that a message was saved.
		parts = urlsplit(url)
		query = [(k, v) for k, v in parse_qsl(parts.query) if k != "wait"]
		query.append(("wait", "true"))
		url = urlunsplit(parts._replace(query=urlencode(query)))
	try:
		response = requests.post(url, json=payload, timeout=config.request_timeout_seconds, allow_redirects=False)
	except requests.RequestException as exc:
		raise WebhookError(f"Webhook request failed ({type(exc).__name__}); delivery is unconfirmed.") from None
	if response.status_code == 429:
		delay = _retry_after_seconds(response)
		if webhook_type == "discord":
			try:
				body_delay = float(response.json().get("retry_after", 0))
				if math.isfinite(body_delay):
					delay = max(delay, body_delay)
			except (TypeError, ValueError, AttributeError):
				pass
		raise TemporaryBlockedError(429, delay, "Discord" if webhook_type == "discord" else "Webhook")
	if not 200 <= response.status_code < 300:
		raise WebhookError(
			f"Webhook returned HTTP {response.status_code}; delivery is unconfirmed.",
			retryable=response.status_code not in (401, 403, 404),
		)
	if webhook_type == "discord":
		try:
			confirmed = bool(response.json().get("id"))
		except (ValueError, AttributeError):
			confirmed = False
		if not confirmed:
			raise WebhookError("Discord did not return a message ID; delivery is unconfirmed.")


def _compute_blocked_backoff_seconds(config: Config, blocked_attempts: int) -> int:
	window_max = min(
		config.blocked_backoff_max_seconds,
		config.blocked_backoff_min_seconds * (2 ** min(blocked_attempts, 20)),
	)
	return random.randint(config.blocked_backoff_min_seconds, window_max)


def _iter_alerts(previous: Optional[Availability], current: Availability) -> Iterable[str]:
	if previous is None:
		return []

	alerts: list[str] = []

	if current.seats.remaining > 0 and current.seats.remaining != previous.seats.remaining:
		if current.waitlist.actual > 0:
			alerts.append("waitlist_lockout")
		else:
			alerts.append("seat_open")

	if (
		current.seats.remaining == 0
		and current.waitlist.remaining > 0
		and current.waitlist.remaining != previous.waitlist.remaining
	):
		alerts.append("waitlist_open")

	return alerts


def run() -> None:
	config = Config.from_env()
	logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s %(levelname)s %(message)s",
	)

	session = requests.Session()
	state: Dict[str, Availability] = {}
	blocked_attempts = 0

	logging.info("Starting OSCAR notifier for term %s and CRNs=%s", config.term_id, ", ".join(config.target_crns))

	while True:
		cycle_backoff: Optional[float] = None
		for crn in config.target_crns:
			try:
				current = fetch_availability(session, config, crn)
				previous = state.get(crn)

				for alert_type in _iter_alerts(previous, current):
					send_alert(config, crn, alert_type, current)
					logging.info("Alert sent for CRN %s (%s)", crn, alert_type)

				state[crn] = current

			except TemporaryBlockedError as exc:
				blocked_attempts += 1
				cycle_backoff = max(exc.retry_after, _compute_blocked_backoff_seconds(config, blocked_attempts))
				logging.warning("%s returned %s for CRN %s. Backing off for %s seconds.", exc.service, exc.status_code, crn, cycle_backoff)
				# A service-wide limit applies to the remaining CRNs too.
				break

			except WebhookError as exc:
				logging.error("Alert for CRN %s failed: %s", crn, exc)
				if not exc.retryable:
					logging.error("Stopping: update WEBHOOK_URL or the Discord channel permissions before restarting.")
					return

			except (requests.Timeout, requests.ConnectionError) as exc:
				logging.warning("Network issue for CRN %s: %s", crn, exc)

			except ParseError as exc:
				logging.error("Parse issue for CRN %s: %s", crn, exc)

			except requests.HTTPError as exc:
				logging.error("HTTP error for CRN %s: %s", crn, exc)

			except Exception:
				logging.exception("Unexpected error while processing CRN %s", crn)

		if config.run_once:
			logging.info("RUN_ONCE enabled. Exiting after one monitoring cycle.")
			break

		if cycle_backoff is None:
			blocked_attempts = 0
		sleep_for = cycle_backoff if cycle_backoff is not None else random.randint(config.poll_min_seconds, config.poll_max_seconds)
		logging.info("Cycle complete. Sleeping for %s seconds.", sleep_for)
		time.sleep(sleep_for)


if __name__ == "__main__":
	try:
		run()
	except KeyboardInterrupt:
		logging.info("Notifier stopped by user.")
