# Georgia Tech OSCAR Notifier

A local Python app that watches Georgia Tech course availability in OSCAR and sends Discord webhook notifications when seat or waitlist counts change to an available state.

It reads the public Banner class-search enrollment endpoint. No Georgia Tech login is needed for the current public endpoint, and the app does not register for classes.

## Setup

Use Python 3.9 or newer. From the project directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
```

Edit `.env`:

```dotenv
WEBHOOK_URL=https://discord.com/api/webhooks/your_webhook_id/your_webhook_token
TERM_ID=202702
TARGET_CRNS=12345,23456
WEBHOOK_TYPE=discord
```

Replace the webhook and example CRNs with your own. The webhook posts to its configured Discord channel. Keep its URL in `.env`, which Git ignores.

`TERM_ID` combines the four-digit year with a semester code:

| Semester | Suffix | Example |
| --- | --- | --- |
| Spring | `02` | `202702` |
| Summer | `05` | `202705` |
| Fall | `08` | `202708` |

Use CRNs from the selected term in [OSCAR's class search](https://registration.banner.gatech.edu/StudentRegistrationSsb/ssb/term/termSelection?mode=search). CRNs can change between terms, and a future term may not be available yet.

The app loads `.env` from the directory containing `notifierscript.py`. Existing environment variables take precedence. Restart the process after changing configuration.

## Run

```sh
.venv/bin/python main.py
```

Stop a foreground run with **Ctrl+C**. Keep the computer awake and connected to the internet while monitoring. Run only one copy to avoid duplicate alerts.

To keep it running after closing the terminal on macOS or Linux, first stop any foreground copy, then run:

```sh
nohup .venv/bin/python -u main.py >> notifier.log 2>&1 &
```

The shell prints the process ID. Stop that process with `kill PROCESS_ID`, replacing `PROCESS_ID` with the printed number. A background run does not restart automatically after a reboot.

Follow the background log with:

```sh
tail -f notifier.log
```

For a single read-only polling cycle:

```sh
RUN_ONCE=true DRY_RUN=true .venv/bin/python main.py
```

This checks the configured classes without sending notifications. It establishes the initial baseline, so it does not produce a sample Discord message.

## Alert behavior

- The first successful check establishes a baseline and does not send an alert, even if a spot is already open.
- Later checks notify when the number of available seats changes and the new count is positive. This includes decreases that leave seats available.
- When seats are available but students remain on the waitlist, the message notes that the waitlist may take priority.
- A waitlist alert is sent when regular seats are exactly zero, available waitlist spots are positive, and that waitlist availability count changed.
- Unchanged counts do not trigger repeat alerts. Counts changing to zero do not trigger closure alerts.
- Previous counts are held in memory and reset on restart. There is no persistent alert history or startup notification.

An alert reports observed availability; registration restrictions and waitlist priority still apply. Register through OSCAR yourself.

## Polling and failure handling

The default delay between cycles is randomly chosen between 30 and 45 seconds. `.env.example` documents the polling, timeout, backoff, dry-run, and single-cycle options.

The client checks both the guest-session bootstrap and enrollment responses. OSCAR HTTP 403, 429, and 503 responses pause the remaining classes for that cycle. Backoff honors server-provided `Retry-After` values, including delays longer than the configured random backoff ceiling. Network and parsing failures are logged; unavailable data is not substituted with zero seats.

Discord requests use `wait=true` and require a returned message ID to confirm delivery. Rate limits honor Discord's retry delay. A webhook HTTP 401, 403, or 404 stops monitoring so the webhook or permissions can be corrected. Webhook exception logs omit the secret URL. If a delivery times out after Discord accepts it, a later retry can produce a duplicate.

The script retains its original Telegram and generic webhook payload options; Discord is the documented setup here.

## Tests

```sh
.venv/bin/python -m unittest -v test_notifierscript
```

The tests mock network calls and do not send Discord messages. They cover the current enrollment response format, missing data, redirects, bootstrap failures, rate limits, delivery confirmation, credential-safe errors, dry runs, and configuration loading.

## Project files

| File | Purpose |
| --- | --- |
| `main.py` | Entry point |
| `notifierscript.py` | Configuration, OSCAR parsing, polling, and webhook delivery |
| `test_notifierscript.py` | Backend compatibility tests |
| `.env.example` | Configuration template with placeholder credentials |
| `requirements.txt` | Python dependencies |

Local `.env` files, virtual environments, logs, PID files, and backups are excluded from Git.
