# Progress Tracker

| Week | Focus | Status |
|------|-------|--------|
| Week 1 | Environment Setup | Done |
| Week 2 | Practical Tools | Done |
| Week 3 | Agents Architecture + Deployment | Done |
| Infrastructure Hardening | HTTP Basic Auth + production WSGI server | Done |

**Week 1 summary:** Set up git/GitHub workflow, called a weather API (Open-Meteo) with `requests`, practiced functions and conditional logic, authenticated with Google Calendar via OAuth, and built a morning briefing script combining weather + calendar data.

**Week 2 summary:** Added try/except error handling, integrated Gmail (unread emails) alongside Calendar OAuth, built a SQLite training log, and combined every data source into a Flask dashboard.

**Week 3 summary:** Refactored into a modular `agents/` structure, added a real Claude API insight for the calendar agent, and got the dashboard live-deployed on Render, accessible from a phone.

**Infrastructure Hardening summary:** Added HTTP Basic Auth protecting the whole dashboard (env-var credentials, fails closed), and replaced the Flask dev server with Gunicorn (single worker, to keep the in-memory API caches consistent) as the production WSGI server.
