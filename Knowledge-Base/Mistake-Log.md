# Mistake Log

- Day 4 - UnicodeEncodeError when printing emoji on Windows console (cp1255 codepage). Fix: force UTF-8 stdout. Lesson: Windows terminal encoding can silently break scripts with non-ASCII characters.
- 16 commits were made but never pushed to GitHub — "commit" and "push" are separate steps; Render only sees what's pushed.
- Render's /etc/secrets/ is read-only — refreshed OAuth tokens must be written to a different writable path (tempfile dir), not back to the secrets mount.
- gitignored local files (like a SQLite db) don't exist on a fresh deploy — code must create tables/data on first run, not assume they exist.
