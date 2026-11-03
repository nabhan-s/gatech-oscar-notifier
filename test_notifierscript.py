"""Backend compatibility checks. All notification requests are mocked."""
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests
import notifierscript as app


def config(**overrides):
    base = app.Config(
        webhook_url="https://discord.com/api/webhooks/123/test-token",
        webhook_type="auto", term_id="202608", target_crns=["92195", "12345"],
        poll_min_seconds=30, poll_max_seconds=45, request_timeout_seconds=10,
        blocked_backoff_min_seconds=300, blocked_backoff_max_seconds=900,
        user_agent=app.DEFAULT_USER_AGENT, run_once=True, dry_run=False,
    )
    return replace(base, **overrides)


def response(status=200, body="", headers=None, url=app.CLASS_SEARCH_BOOTSTRAP_URL):
    result = requests.Response()
    result.status_code = status
    result._content = body.encode()
    result.headers.update(headers or {})
    result.url = url
    return result


def enrollment(actual="78", capacity="78", remaining="0"):
    # Same labels and span structure as OSCAR's live getEnrollmentInfo response.
    return f"""<section aria-labelledby="enrollmentInfo">
    <span>Enrollment Actual:</span><span>{actual}</span><br>
    <span>Enrollment Maximum:</span><span>{capacity}</span><br>
    <span>Enrollment Seats Available:</span><span>{remaining}</span><br><hr>
    <span>Waitlist Capacity:</span><span>0</span><br>
    <span>Waitlist Actual:</span><span>0</span><br>
    <span>Waitlist Seats Available:</span><span>0</span><br></section>"""


class BackendTests(unittest.TestCase):
    def test_current_enrollment_and_formatted_counts(self):
        data = app.parse_enrollment_info(enrollment())
        self.assertEqual(data.seats, app.RowAvailability(78, 78, 0))
        self.assertEqual(data.waitlist.remaining, 0)
        data = app.parse_enrollment_info(enrollment("1,200", "1,199", "-1"))
        self.assertEqual(data.seats, app.RowAvailability(1199, 1200, -1))

    def test_missing_data_is_not_reported_as_zero(self):
        with self.assertRaises(app.ParseError):
            app.parse_enrollment_info("<html>Maintenance: please try again later</html>")

    def test_bootstrap_failure_stops_before_enrollment_request(self):
        for status in (403, 429, 503):
            with self.subTest(status=status):
                session = Mock()
                session.get.return_value = response(status, headers={"Retry-After": "1200"})
                with self.assertRaises(app.TemporaryBlockedError) as caught:
                    app.fetch_availability(session, config(), "92195")
                self.assertEqual(caught.exception.retry_after, 1200)
                self.assertEqual(session.get.call_count, 1)

    def test_login_redirect_is_unavailable_data(self):
        session = Mock()
        session.get.return_value = response(url="https://login.gatech.edu/cas/login")
        with self.assertRaises(app.ParseError):
            app.fetch_availability(session, config(), "92195")

    def test_fetch_current_response(self):
        session = Mock()
        session.get.side_effect = [response(), response(body=enrollment())]
        self.assertEqual(app.fetch_availability(session, config(), "92195").seats.actual, 78)
        self.assertIn("term=202608&courseReferenceNumber=92195", session.get.call_args.args[0])

    def test_retry_after_http_date_and_malformed_value(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120))
        delay = app._retry_after_seconds(response(headers={"Retry-After": future}))
        self.assertTrue(118 <= delay <= 120)
        for value in ("bad", "-1", "inf", "nan", ""):
            self.assertEqual(app._retry_after_seconds(response(headers={"Retry-After": value})), 0)

    def test_discord_wait_confirmation_preserves_thread(self):
        cfg = config(webhook_url="https://discord.com/api/v10/webhooks/123/test-token?thread_id=456&wait=false")
        self.assertEqual(app._resolve_webhook_type(cfg), "discord")
        with patch.object(app.requests, "post", return_value=response(body='{"id":"789"}')) as post:
            app.send_alert(cfg, "92195", "seat_open", app.parse_enrollment_info(enrollment(remaining="1")))
        query = parse_qs(urlsplit(post.call_args.args[0]).query)
        self.assertEqual(query, {"thread_id": ["456"], "wait": ["true"]})
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_discord_requires_message_id(self):
        for status, body in ((204, ""), (200, "{}"), (200, "[]")):
            with self.subTest(status=status, body=body), patch.object(app.requests, "post", return_value=response(status, body)):
                with self.assertRaises(app.WebhookError):
                    app.send_alert(config(), "92195", "seat_open", app.parse_enrollment_info(enrollment()))

    def test_discord_rate_limit_honors_larger_body_delay(self):
        limited = response(429, json.dumps({"retry_after": 1400.5}), {"Retry-After": "1000"})
        with patch.object(app.requests, "post", return_value=limited):
            with self.assertRaises(app.TemporaryBlockedError) as caught:
                app.send_alert(config(), "92195", "seat_open", app.parse_enrollment_info(enrollment()))
        self.assertEqual(caught.exception.retry_after, 1400.5)
        self.assertEqual(caught.exception.service, "Discord")

    def test_failed_webhook_does_not_expose_token(self):
        with patch.object(app.requests, "post", side_effect=requests.ConnectionError(config().webhook_url)):
            with self.assertRaises(app.WebhookError) as caught:
                app.send_alert(config(), "92195", "seat_open", app.parse_enrollment_info(enrollment()))
        self.assertNotIn("test-token", str(caught.exception))

    def test_deleted_or_unauthorized_webhook_is_permanent_failure(self):
        for status in (401, 403, 404):
            with self.subTest(status=status), patch.object(app.requests, "post", return_value=response(status)):
                with self.assertRaises(app.WebhookError) as caught:
                    app.send_alert(config(), "92195", "seat_open", app.parse_enrollment_info(enrollment()))
                self.assertFalse(caught.exception.retryable)

    def test_dry_run_never_posts(self):
        with patch.object(app.requests, "post") as post:
            app.send_alert(config(dry_run=True), "92195", "seat_open", app.parse_enrollment_info(enrollment()))
        post.assert_not_called()

    def test_run_once_limit_stops_remaining_crns_without_sleep(self):
        with patch.object(app.Config, "from_env", return_value=config()), \
                patch.object(app, "fetch_availability", side_effect=app.TemporaryBlockedError(429, 1200)) as fetch, \
                patch.object(app.time, "sleep") as sleep:
            app.run()
        self.assertEqual(fetch.call_count, 1)
        sleep.assert_not_called()

    def test_monitor_waits_at_least_server_retry_after(self):
        with patch.object(app.Config, "from_env", return_value=config(run_once=False)), \
                patch.object(app, "fetch_availability", side_effect=app.TemporaryBlockedError(429, 1200)) as fetch, \
                patch.object(app.time, "sleep", side_effect=KeyboardInterrupt) as sleep:
            with self.assertRaises(KeyboardInterrupt):
                app.run()
        self.assertEqual(fetch.call_count, 1)
        sleep.assert_called_once_with(1200)

    def test_env_path_is_anchored_to_script(self):
        values = {"WEBHOOK_URL": config().webhook_url, "TERM_ID": "202608", "TARGET_CRNS": "92195"}
        with patch.dict(app.os.environ, values, clear=True), patch.object(app, "load_dotenv") as load:
            app.Config.from_env()
        load.assert_called_once_with(Path(app.__file__).resolve().parent / ".env")


if __name__ == "__main__":
    unittest.main()
