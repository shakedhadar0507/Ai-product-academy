# Week 2 Summary

## What was built

- **Error handling** — updated the morning briefing script to wrap the weather and calendar calls in try/except so a failure in one source doesn't crash the whole script.
- **Gmail integration** — added a script that authenticates with a combined Calendar + Gmail readonly scope and lists the 5 most recent unread emails (sender + subject).
- **SQLite training log** — a script using the built-in `sqlite3` module to store runs (date, distance, duration, notes) in a local database and print a summary with total distance.
- **Flask dashboard** — a single-page Flask app combining all four data sources (weather, calendar, unread Gmail, training log) into one page, each section independently wrapped in try/except.

## Real bugs hit and fixed

- **Windows console encoding (UnicodeEncodeError)** — printing an emoji in a calendar event title crashed the script because the Windows terminal was using the cp1255 codepage instead of UTF-8. Fixed by forcing `sys.stdout.reconfigure(encoding="utf-8")`.
- **Gmail API not enabled** — the OAuth consent succeeded but every Gmail call failed with a 403 because the Gmail API wasn't enabled on the Google Cloud project tied to the credentials. Fixed by enabling it in Google Cloud Console; no code change needed.
- **Flask working-directory path bug** — the Flask app was first started with its working directory set to `Foundations/`, so the relative paths to `credentials.json`, `token.json`, and `training_log.db` (which live at the repo root) failed to resolve. Fixed by running the app from the repo root instead.
