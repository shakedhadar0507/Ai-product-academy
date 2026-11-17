"""Health check for every part of Shaked Hub, with a phone alert only when something breaks.

Runs from the background loop every CHECK_EVERY seconds (and on demand from /tasks/health).
Each check returns None when fine, or a short Hebrew reason that says what broke and what to
do. Each open problem is pushed once a day (as soon as it is found, or at the first check
after 07:00; never at night). Nothing is sent while everything works.

If pushes themselves are broken (or Supabase, where the devices live), the alert goes by
email from his Gmail instead. If the server is down, nothing here can run: the GitHub
workflow health.yml fails and GitHub emails him.

State (open problems, when each was last pushed) lives in the settings row "config:health",
so a restart never repeats an alert.
"""
import base64
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import anthropic
import requests
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build

from agents import chat_agent, formatting, garmin_agent, garmin_sync_agent, google_auth, push_agent, uni_agent

CHECK_EVERY = 2 * 60 * 60
QUIET_FROM, QUIET_UNTIL = 23, 7   # no alerts at night; they wait for the first check after 07:00
GARMIN_STALE_DAYS = 2
PUSH_STALE_HOURS = 30   # the digest pushes at 07:00 and 21:00, so a day without any push is odd
DASHBOARD_URL = "https://ai-product-academy.onrender.com/"
ANTHROPIC_BILLING = "https://console.anthropic.com/settings/billing"

_last = {"result": None, "running": False}


def _retry(fn):
    """Run a check twice (20 s apart) so one network hiccup never wakes him up."""
    first = fn()
    if first is None:
        return None
    time.sleep(20)
    return fn()


# ---------- Checks: None = ok, otherwise (reason, link) ----------

def check_anthropic():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ("אין מפתח Anthropic בשרת (ANTHROPIC_API_KEY ב-Render)", None)
    try:
        # One token from the model the chat uses: catches no credit, bad key and a retired model.
        anthropic.Anthropic(timeout=40, max_retries=1).messages.create(
            model=chat_agent.MODEL, max_tokens=1, messages=[{"role": "user", "content": "ok"}]
        )
        return None
    except anthropic.AuthenticationError:
        return ("מפתח Anthropic לא תקין. צ'אט, סיכום יומי ומיילים לא עובדים", None)
    except anthropic.PermissionDeniedError as e:
        return (f"Anthropic חסם את המפתח ({str(e)[:60]})", None)
    except anthropic.NotFoundError:
        return (f"המודל {chat_agent.MODEL} כבר לא זמין. צריך לעדכן מודל בקוד", None)
    except anthropic.BadRequestError as e:
        if "credit" in str(e).lower():
            return ("נגמר הקרדיט ב-Anthropic. צ'אט, סיכום יומי ומיילים לא עובדים עד שתטען", ANTHROPIC_BILLING)
        if "usage limit" in str(e).lower():
            return ("הגעת לתקרת ההוצאה החודשית ב-Anthropic. ה-AI באפליקציה חסום עד החודש הבא או עד שתעלה את התקרה",
                    "https://console.anthropic.com/settings/limits")
        return (f"Anthropic דחה בקשה: {str(e)[:80]}", None)
    except anthropic.APIStatusError as e:
        if e.status_code == 429 and "credit" in str(e).lower():
            return ("נגמר הקרדיט ב-Anthropic", ANTHROPIC_BILLING)
        return (f"Anthropic לא עונה (שגיאה {e.status_code})", None)
    except Exception as e:
        return (f"Anthropic לא עונה ({type(e).__name__})", None)


def check_google():
    try:
        creds = google_auth.get_credentials(google_auth.SCOPES)
        build("gmail", "v1", credentials=creds).users().labels().list(userId="me").execute()
        build("calendar", "v3", credentials=creds).calendarList().list(maxResults=1).execute()
        return None
    except RefreshError:
        return ("Google התנתק: יומן ומיילים לא נטענים. צריך להריץ reauth_google.py במחשב ולהעלות token.json ל-Render", None)
    except Exception as e:
        return (f"Google (יומן/מייל) לא עונה ({type(e).__name__})", None)


def check_supabase():
    if not push_agent.is_configured():
        return ("Supabase לא מוגדר בשרת (SUPABASE_URL / SUPABASE_SECRET_KEY)", None)
    try:
        push_agent.device_count()
        return None
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        return (f"Supabase מחזיר שגיאה {code}. גרמין, משימות והתראות תלויים בו", "https://supabase.com/dashboard")
    except Exception as e:
        return (f"Supabase לא זמין ({type(e).__name__}). אולי הפרויקט הושהה", "https://supabase.com/dashboard")


def check_moodle():
    if not uni_agent.is_configured():
        return ("קישור Moodle לא מוגדר (MOODLE_ICAL_URL ב-Render). אין התראות על מטלות", None)
    try:
        uni_agent._events_cache["events"] = None  # read it fresh, not from the 5-minute cache
        uni_agent.get_upcoming()
        return None
    except ValueError:
        return ("קישור Moodle לא מחזיר יומן. צריך קישור ייצוא חדש (export_execute.php) ב-Render", None)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        return (f"Moodle מחזיר שגיאה {code}. אולי הקישור פג או שנה אקדמית חדשה", None)
    except Exception as e:
        return (f"Moodle לא עונה ({type(e).__name__})", None)


def check_garmin():
    if not garmin_agent.is_configured():
        return None  # no Supabase: reported by the Supabase check
    if not garmin_sync_agent.is_configured():
        return ("אין טוקן גרמין בשרת. צריך להריץ upload_garmin_token.py במחשב", None)
    last = garmin_sync_agent.status().get("last")
    if last and not last.get("ok"):
        error = last.get("error", "")
        if "401" in error or "403" in error or "Auth" in error:
            return ("טוקן גרמין פג. צריך להריץ upload_garmin_token.py במחשב", None)
        return (f"סנכרון גרמין נכשל ({error[:60]})", None)
    # Rows exist for every day even when the watch sent nothing, so look for real readings.
    rows = garmin_agent._query("garmin_daily_wellness", {
        "select": "date", "resting_hr": "not.is.null", "order": "date.desc", "limit": "1"})
    if not rows:
        return ("אין נתוני גרמין בכלל ב-Supabase", None)
    newest = datetime.fromisoformat(rows[0]["date"]).date()
    days = (formatting.today_in_tz() - newest).days
    if days > GARMIN_STALE_DAYS:
        return (f"אין נתונים מהשעון מאז {newest.strftime('%d/%m')}. צריך לסנכרן את השעון באפליקציית Garmin Connect", None)
    return None


def check_weather():
    """Weather is fine if either source answers (Open-Meteo rate-limits Render often)."""
    lat, lon = 32.08, 34.78
    try:
        chat_agent._open_meteo(lat, lon, None)
        return None
    except Exception:
        pass
    try:
        chat_agent._met_norway(lat, lon, None)
        return None
    except Exception as e:
        return (f"מזג אוויר לא נטען משני המקורות ({type(e).__name__})", None)


def check_push():
    """Can pushes go out? Key valid, a phone subscribed, and the last real push reached it."""
    problem = push_agent.private_key_problem()
    if problem:
        return (f"התראות לא יכולות לצאת: {problem}", None)
    if push_agent.device_count() == 0:
        return ("אין אף טלפון רשום להתראות. צריך לפתוח את האפליקציה ולאשר התראות", DASHBOARD_URL)
    last = push_agent.last_send()
    if last and last["devices"] and last["sent"] == 0:
        return (f"ההתראה האחרונה ({last['at']}) לא הגיעה לאף מכשיר: {last.get('error') or 'נדחתה'}", DASHBOARD_URL)
    return None


CHECKS = {
    "anthropic": ("Claude", check_anthropic),
    "google": ("Google", check_google),
    "supabase": ("Supabase", check_supabase),
    "push": ("התראות", check_push),
    "moodle": ("Moodle", check_moodle),
    "garmin": ("גרמין", check_garmin),
    "weather": ("מזג אוויר", check_weather),
}


def run_checks(retry=True):
    results = {}
    for name, (_label, fn) in CHECKS.items():
        try:
            outcome = _retry(fn) if retry else fn()
        except Exception as e:
            outcome = (f"הבדיקה עצמה נכשלה ({type(e).__name__})", None)
        results[name] = outcome
    return results


# ---------- Alerting ----------

def _send_email(subject, body):
    """Fallback when pushes can't go out: an email from his Gmail to himself."""
    creds = google_auth.get_credentials(google_auth.SCOPES)
    gmail = build("gmail", "v1", credentials=creds)
    me = gmail.users().getProfile(userId="me").execute()["emailAddress"]
    message = MIMEText(body, "plain", "utf-8")
    message["to"], message["from"], message["subject"] = me, me, subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()


def _alert(problems):
    """problems: list of (name, reason, link). Returns how the alert went out."""
    labels = [CHECKS[n][0] for n, _, _ in problems]
    title = "⚠️ Shaked Hub: " + (problems[0][1].split(".")[0] if len(problems) == 1 else f"{len(problems)} תקלות")
    if len(problems) == 1:
        body = problems[0][1]
    else:
        body = " · ".join(f"{label}: {reason.split('.')[0]}" for label, (_, reason, _) in zip(labels, problems))
    link = next((l for _, _, l in problems if l), DASHBOARD_URL)
    channel = {"push": 0, "email": False}
    push_broken = any(n in ("push", "supabase") for n, _, _ in problems)
    if not push_broken:
        try:
            channel["push"] = push_agent.send_to_all(title[:60], body[:230], link)
        except Exception as e:
            print(f"[ERROR] health push: {type(e).__name__}: {e}", file=sys.stderr)
    if channel["push"] == 0:
        try:
            text = "\n\n".join(f"{CHECKS[n][0]}: {reason}" + (f"\n{l}" if l else "") for n, reason, l in problems)
            _send_email(title, text + "\n\n(נשלח במייל כי התראות לטלפון לא עובדות כרגע.)")
            channel["email"] = True
        except Exception as e:
            print(f"[ERROR] health email: {type(e).__name__}: {e}", file=sys.stderr)
    return channel


def _state():
    try:
        return push_agent.get_config("health") or {}
    except Exception:
        return {}


def run(test=False):
    """Run every check, alert on problems (once a day each, never at night), save state.
    test=True also sends one push even when all is fine, to prove the phone gets alerts."""
    started = time.time()
    now = formatting.now_in_tz()
    today = now.date().isoformat()
    quiet = now.hour < QUIET_UNTIL or now.hour >= QUIET_FROM
    results = run_checks()
    state = _state()
    open_before = state.get("open", {})

    to_alert = []
    open_now = {}
    for name, outcome in results.items():
        if outcome is None:
            continue
        reason, link = outcome
        previous = open_before.get(name) or {}
        alerted_on = previous.get("alerted")
        if test or (not quiet and alerted_on != today):
            to_alert.append((name, reason, link))
            alerted_on = today
        open_now[name] = {"reason": reason, "since": previous.get("since", now.strftime("%d/%m %H:%M")),
                          "alerted": alerted_on}

    channel = None
    if to_alert:
        channel = _alert(to_alert)
    elif test:
        sent = push_agent.send_to_all("✅ Shaked Hub: בדיקת התראה", "כל הרכיבים תקינים. ככה תיראה התראה כשמשהו נשבר.", DASHBOARD_URL)
        channel = {"push": sent, "email": False}
    report = {
        "at": now.strftime("%d/%m %H:%M"),
        "started_ts": started,
        "seconds": round(time.time() - started),
        "ok": not open_now,
        "checks": {n: ("ok" if o is None else o[0]) for n, o in results.items()},
        "alerted": [n for n, _, _ in to_alert],
        "held_for_morning": quiet and bool(open_now) and not to_alert,
        "channel": channel,
    }
    try:
        push_agent.set_config("health", {"open": open_now, "last": report})
    except Exception as e:
        print(f"[ERROR] health state: {type(e).__name__}", file=sys.stderr)
    _last["result"] = report
    return report


def run_safely(test=False):
    if _last["running"]:
        return None
    _last["running"] = True
    try:
        return run(test)
    except Exception as e:
        print(f"[ERROR] health: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    finally:
        _last["running"] = False


def is_running():
    return _last["running"]


def last_result():
    return _last["result"] or _state().get("last")
