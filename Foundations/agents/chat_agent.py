"""Ask Claude as a chat: a running conversation that can look at and act on everything the
Hub knows (calendar, mail, tasks and reminders, training).

It never sends mail: replies and new mails are saved as Gmail drafts. The conversation (text only,
last MAX_TURNS messages) is kept in the Supabase settings row "config:chat", so it survives restarts
and is the same on phone and computer.
"""
import hashlib
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone

import time

import anthropic
import requests
from googleapiclient.discovery import build

from agents import (action_agent, calendar_agent, formatting, garmin_agent, gmail_agent, google_auth,
                    location_agent, push_agent, uni_agent, weather_agent)

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TURNS = 20
MAX_STEPS = 10

TOOLS = action_agent.TOOLS + [
    {
        "name": "get_calendar",
        "description": "Events in the user's Google Calendar, with their ids (needed to update or delete). Also use it to check whether an event already exists before creating one.",
        "input_schema": {"type": "object", "properties": {
            "days_ahead": {"type": "integer", "description": "First day: 0 = today, 1 = tomorrow, up to 60"},
            "days_span": {"type": "integer", "description": "How many days from that day, default 1, up to 14"},
            "query": {"type": "string", "description": "Optional text to match in the title, place or description"},
        }, "required": ["days_ahead"]},
    },
    {
        "name": "update_calendar_event",
        "description": "Change an existing event by id from get_calendar: title, date, times, place or description. Only the fields given change; moving the start keeps the length unless end_time is given.",
        "input_schema": {"type": "object", "properties": {
            "event_id": {"type": "string"},
            "summary": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "start_time": {"type": "string", "description": "HH:MM, 24-hour"},
            "end_time": {"type": "string", "description": "HH:MM, 24-hour"},
            "location": {"type": "string"},
            "description": {"type": "string"},
        }, "required": ["event_id"]},
    },
    {
        "name": "delete_calendar_event",
        "description": "Delete one event by id from get_calendar. Only when he asks to delete or cancel it.",
        "input_schema": {"type": "object", "properties": {"event_id": {"type": "string"}}, "required": ["event_id"]},
    },
    {
        "name": "get_weather",
        "description": "Current weather and the next hours where he is now (his phone's location), or at given coordinates.",
        "input_schema": {"type": "object", "properties": {
            "lat": {"type": "number"}, "lon": {"type": "number"},
        }},
    },
    {
        "name": "search_mail",
        "description": "Search the user's Gmail with Gmail search syntax (e.g. 'is:unread', 'from:moodle newer_than:3d'). Returns up to 8 mails with id, sender, subject, date and snippet.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "read_mail",
        "description": "The full text of one mail, by id from search_mail.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "list_tasks",
        "description": "The user's open tasks and reminders (Moodle deadlines, tasks from uni mail, reminders from his Notes or added here), with keys and due times.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_reminder",
        "description": "Add a task with a time. He gets a phone push at that time and it shows in the tasks card.",
        "input_schema": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Short, in his language"},
            "when": {"type": "string", "description": "YYYY-MM-DDTHH:MM, Israel time"},
        }, "required": ["title", "when"]},
    },
    {
        "name": "complete_task",
        "description": "Mark a task done, by its key from list_tasks.",
        "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
    },
    {
        "name": "get_training",
        "description": "Garmin: last night's sleep, HRV, readiness, today's planned workout and recent runs.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

SYSTEM = """You are Shaked's personal assistant inside Shaked Hub, his command-center app. He is a
Reichman University business and entrepreneurship student and a runner. Answer in the language he
writes in (usually Hebrew), short and direct, plain text with no markdown, asterisks or headings.

How you act:
- Use the tools to look things up instead of guessing, and to act when he asks.
- Act only on his newest message. Every earlier message was already handled, whatever it asked for:
  replies that did something end with a ✅ receipt line from the app. Never redo an action from an
  earlier message.
  If he asks to change something you created, update it (update_calendar_event), do not create it again.
- Never say you did something unless a tool call in this turn returned success for it. If a tool
  failed, say it failed. If no tool can do what he asks, say so plainly. The app appends a receipt of
  the real actions to your reply, so do not repeat the event details in a list.
- Before creating events or reminders, make sure the date and time are clear; if not, ask.
- Mail is never sent: you can only save Gmail drafts, so say "saved as a draft".

Now is {now} ({weekday}), in time zone {tz}.
{location}"""

WRITE_TOOLS = {"create_calendar_event", "update_calendar_event", "delete_calendar_event",
               "create_gmail_draft", "add_reminder", "complete_task"}
CLAIM_WORDS = re.compile(r"(?<!לא )(?<!not )(יצרתי|נוצר|קבעתי|הוספתי|הכנסתי|שמרתי|עדכנתי|מחקתי|הזזתי|סימנתי|\b(created|scheduled|added|saved|updated|deleted|moved)\b)", re.I)
RECEIPT_LINE = re.compile(r"^\s*(✅|⚠️|\[).*(\]\s*)?$", re.M)
DEADLINE_SECONDS = 95


def _gmail():
    return build("gmail", "v1", credentials=google_auth.get_credentials(google_auth.SCOPES))


def get_calendar(days_ahead, days_span=1, query=None, tz=formatting.DEFAULT_TZ_NAME):
    days = max(0, min(int(days_ahead), 60))
    span = max(1, min(int(days_span or 1), 14))
    events = calendar_agent.list_events(tz, days, query or None, span) or []
    out = []
    for e in events:
        start, end = e.get("start", {}), e.get("end", {})
        out.append({
            "id": e.get("id"),
            "date": (start.get("dateTime") or start.get("date") or "")[:10],
            "time": start.get("dateTime", "")[11:16] or "all day",
            "end": end.get("dateTime", "")[11:16],
            "title": e.get("summary", ""),
            "location": e.get("location", ""),
            "recurring": bool(e.get("recurringEventId")),
        })
    return out


def get_weather(lat=None, lon=None, here=None):
    if lat is None or lon is None:
        if not here:
            return "His location is unknown (location permission is off). Ask him where he is."
        lat, lon = here["lat"], here["lon"]
    try:
        return _open_meteo(lat, lon, here)
    except Exception as e:
        print(f"[WARN] chat weather open-meteo: {type(e).__name__}: {e}", file=sys.stderr)
        return _met_norway(lat, lon, here)


def _met_norway(lat, lon, here):
    """Backup source (MET Norway), used when Open-Meteo refuses (it rate-limits Render's shared IP)."""
    r = requests.get("https://api.met.no/weatherapi/locationforecast/2.0/compact",
                     params={"lat": round(lat, 3), "lon": round(lon, 3)}, headers=weather_agent.HEADERS, timeout=10)
    r.raise_for_status()
    series = r.json()["properties"]["timeseries"]
    def row(t):
        d = t["data"]["instant"]["details"]
        rain = (t["data"].get("next_1_hours") or {}).get("details", {}).get("precipitation_amount")
        return {"time_utc": t["time"][11:16], "temp_c": d.get("air_temperature"),
                "humidity": d.get("relative_humidity"), "wind_kmh": round(d.get("wind_speed", 0) * 3.6, 1),
                "rain_mm_next_hour": rain}
    return {"place": (here or {}).get("place") if lat == (here or {}).get("lat") else None,
            "now": row(series[0]), "next_hours": [row(t) for t in series[1:8]],
            "note": "times are UTC"}


def _open_meteo(lat, lon, here):
    r = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,precipitation",
        "hourly": "temperature_2m,precipitation_probability",
    }, headers=weather_agent.HEADERS, timeout=10)
    r.raise_for_status()
    forecast = r.json()
    cur, hourly = forecast["current"], forecast.get("hourly", {})
    times = hourly.get("time", [])
    i = next((k for k, t in enumerate(times) if t[:13] >= cur.get("time", "")[:13]), 0)
    return {
        "place": (here or {}).get("place") if lat == (here or {}).get("lat") else None,
        "local_time_zone": forecast.get("timezone"),
        "now": {"temp_c": cur["temperature_2m"], "feels_like_c": cur["apparent_temperature"],
                "humidity": cur["relative_humidity_2m"], "wind_kmh": cur["wind_speed_10m"],
                "rain_mm": cur.get("precipitation")},
        "next_hours": [{"time": times[k][11:16], "temp_c": hourly["temperature_2m"][k],
                        "rain_chance": hourly["precipitation_probability"][k]}
                       for k in range(i, min(i + 8, len(times)))],
    }


def search_mail(query):
    service = _gmail()
    listing = service.users().messages().list(userId="me", q=query, maxResults=8).execute()
    out = []
    for m in listing.get("messages", []):
        d = service.users().messages().get(
            userId="me", id=m["id"], format="metadata", metadataHeaders=["Subject", "From", "Date"]
        ).execute()
        h = {x["name"].lower(): x["value"] for x in d["payload"].get("headers", [])}
        out.append({"id": m["id"], "from": h.get("from", ""), "subject": h.get("subject", ""),
                    "date": h.get("date", ""), "snippet": html.unescape(d.get("snippet", ""))})
    return out


def read_mail(id):
    message = gmail_agent.get_message(id)
    return {k: message[k] for k in ("from", "subject", "date")} | {"text": message["text"][:5000]}


def list_tasks():
    return [{
        "key": t["key"], "title": t["title"], "course": t.get("course"), "source": t.get("source"),
        "due": t["due"].astimezone(formatting.ISRAEL_TZ).strftime("%Y-%m-%d %H:%M") if t["due"] else None,
    } for t in uni_agent.list_tasks() if not t["done"]]


def add_reminder(title, when):
    due = datetime.fromisoformat(when)
    due = due.replace(tzinfo=formatting.ISRAEL_TZ) if due.tzinfo is None else due
    if due <= datetime.now(timezone.utc):
        return "That time has already passed."
    key = "manual:" + hashlib.sha256(f"{title}|{due.isoformat()}".encode()).hexdigest()[:20]
    uni_agent._save_tasks([{"key": key, "source": "manual", "kind": "note", "title": title[:80],
                            "course": None, "due": due.isoformat(), "url": None}])
    return f"Saved. Push at {due.strftime('%d/%m %H:%M')}."


def complete_task(key):
    uni_agent.set_done(key, True)
    return "Marked done."


def get_training():
    today = formatting.today_in_tz()
    data = garmin_agent.get_data(today)
    runs = [{"date": r["start_time_local"][:10], "km": round((r["distance_m"] or 0) / 1000, 1),
             "pace": garmin_agent.format_pace(r["avg_pace_s_per_km"]), "avg_hr": r.get("avg_hr")}
            for r in data["runs"][:5]]
    return {"wellness": data["wellness"], "planned_today": garmin_agent.get_planned_workout(today),
            "recent_runs": runs}


def _tool_functions(tz, here):
    return {
        "create_calendar_event": lambda **kw: calendar_agent.create_event(tz_name=tz, **kw),
        "update_calendar_event": lambda event_id, **kw: calendar_agent.update_event(event_id, tz_name=tz, **kw),
        "delete_calendar_event": lambda event_id: calendar_agent.delete_event(event_id),
        "create_gmail_draft": lambda **kw: gmail_agent.create_draft(**kw),
        "get_calendar": lambda **kw: get_calendar(tz=tz, **kw),
        "get_weather": lambda **kw: get_weather(here=here, **kw),
        "search_mail": search_mail, "read_mail": read_mail,
        "list_tasks": list_tasks, "add_reminder": add_reminder, "complete_task": complete_task,
        "get_training": get_training,
    }


def _when(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return iso or ""


def receipt(name, args, output):
    """One plain line describing an action that really happened, built from the tool's own result."""
    o = output if isinstance(output, dict) else {}
    if name == "create_calendar_event":
        if o.get("already_existed"):
            return f"✅ כבר קיים ביומן (לא נוצר שוב): {o.get('summary')} {_when(o.get('start'))}"
        return f"✅ נוצר ביומן: {o.get('summary')} {_when(o.get('start'))}"
    if name == "update_calendar_event":
        return f"✅ עודכן ביומן: {o.get('summary')} {_when(o.get('start'))}"
    if name == "delete_calendar_event":
        return f"✅ נמחק מהיומן: {o.get('summary')} {_when(o.get('start'))}"
    if name == "create_gmail_draft":
        return f"✅ נשמרה טיוטה ב-Gmail: {args.get('subject', '')}"
    if name == "add_reminder":
        if isinstance(output, str) and output.startswith("Saved"):
            return f"✅ תזכורת נשמרה: {args.get('title')} {_when(args.get('when'))}"
        return f"⚠️ התזכורת לא נשמרה: {output}"
    if name == "complete_task":
        return "✅ משימה סומנה כבוצעה"
    return None


def _model_content(m):
    """What the model sees for a saved message: its text as saved. (Markers appended here were
    copied by the model into its own replies, so the rule about earlier messages is in SYSTEM.)"""
    return m["text"]


def history():
    try:
        return (push_agent.get_config("chat") or {}).get("messages", [])
    except Exception:
        return []


def reset():
    push_agent.set_config("chat", {"messages": []})


def clean(text):
    return re.sub(r"[*#`]+", "", text or "").strip()


def _location_line(here):
    if not here:
        return "His current location is unknown (the phone did not share it). If a question depends on where he is, ask."
    place = here.get("place") or "unknown place name"
    return (f"His phone says he is now in {place} (lat {here['lat']}, lon {here['lon']}). "
            "Use this for anything that depends on where he is: weather, distances, travel time, nearby places.")


def ask(user_text, save=True, location=None, past=None):
    """One chat turn. Returns Claude's answer followed by a receipt of the actions that really ran,
    and saves both sides to the history.

    `location` is the phone's report for this message ({"lat", "lon", "accuracy", "tz"}), or None.
    `past` replaces the saved history (used by the self-test so it never touches the real chat)."""
    return turn(user_text, save, location, past)["text"]


def turn(user_text, save=True, location=None, past=None):
    """Like ask(), but returns the saved form {"role", "text", "actions"} of the reply."""
    if past is None:
        past = history()[-MAX_TURNS:]
    while past and past[0]["role"] != "user":
        past = past[1:]
    messages = [{"role": m["role"], "content": _model_content(m)} for m in past]
    messages.append({"role": "user", "content": user_text})

    here = location_agent.current(location, store=save)
    tz = (here or {}).get("tz") or formatting.DEFAULT_TZ_NAME
    now = formatting.now_in_tz(tz)
    system = SYSTEM.format(now=now.strftime("%Y-%m-%d %H:%M"), weekday=now.strftime("%A"), tz=tz,
                           location=_location_line(here))
    client = anthropic.Anthropic(timeout=60, max_retries=1)
    functions, answer, actions, done_keys = _tool_functions(tz, here), "", [], set()
    started, unfinished = time.monotonic(), False
    for step in range(MAX_STEPS):
        response = client.messages.create(
            model=MODEL, max_tokens=4000, system=system, tools=TOOLS, messages=messages,
            output_config={"effort": EFFORT},
        )
        if response.stop_reason == "refusal":
            answer = answer or "לא יכול לעזור עם זה."
            break
        answer = "\n".join(b.text for b in response.content if b.type == "text").strip() or answer
        calls = [b for b in response.content if b.type == "tool_use"]
        if not calls:
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for call in calls:
            key = (call.name, json.dumps(call.input, sort_keys=True, ensure_ascii=False))
            if call.name in WRITE_TOOLS and key in done_keys:
                content, is_error = "Already done earlier in this turn; not repeated.", False
            else:
                try:
                    output = functions[call.name](**call.input)
                    content, is_error = json.dumps(output, ensure_ascii=False, default=str)[:8000], False
                    if call.name in WRITE_TOOLS:
                        done_keys.add(key)
                        line = receipt(call.name, call.input, output)
                        if line:
                            actions.append(line)
                except Exception as e:
                    print(f"[ERROR] chat tool {call.name}: {type(e).__name__}: {e}", file=sys.stderr)
                    content, is_error = f"Error: {type(e).__name__}: {str(e)[:200]}", True
                    if call.name in WRITE_TOOLS:
                        actions.append(f"⚠️ נכשל ({call.name}): {type(e).__name__}")
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})
        if time.monotonic() - started > DEADLINE_SECONDS:
            unfinished = True
            break
    else:
        unfinished = True

    # Receipts come only from the server; drop any the model wrote itself (it copies them from history).
    answer = re.sub(r"\n{3,}", "\n\n", RECEIPT_LINE.sub("", clean(answer))).strip()
    if unfinished:
        answer = (answer + "\n\n" if answer else "") + "⚠️ לא סיימתי את כל השלבים. מה שבוצע בפועל מופיע למטה."
    if not answer and not actions:
        answer = "לא הצלחתי לענות. נסה לנסח אחרת."
    if actions:
        answer = (answer + "\n\n" if answer else "") + "\n".join(actions)
    elif CLAIM_WORDS.search(answer):
        answer += "\n\n⚠️ לא בוצעה בפועל שום פעולה."
    reply = {"role": "assistant", "text": answer, "actions": actions}
    if save:
        saved = past + [{"role": "user", "text": user_text}, reply]
        try:
            push_agent.set_config("chat", {"messages": saved[-MAX_TURNS:]})
        except Exception as e:
            print(f"[ERROR] chat save: {type(e).__name__}", file=sys.stderr)
    return reply


def write_selftest(location=None):
    """Live end-to-end check of the chat's actions on the real calendar, with a throwaway event a
    month ahead that is deleted at the end. The saved chat is never touched.

    Turn 1 creates the event, turn 2 asks something unrelated (it must not create the event again),
    turn 3 moves it (it must update, not create a second one). Then the calendar itself is read."""
    title = "בדיקת Hub " + hashlib.sha256(str(time.time()).encode()).hexdigest()[:4]
    day = (formatting.now_in_tz() + timedelta(days=30)).strftime("%Y-%m-%d")
    report, past, replies = {"turns": []}, [], []
    prompts = [
        f"קבע לי ביומן '{title}' בתאריך {day} מ-07:00 עד 07:15",
        "מה מזג האוויר איפה שאני נמצא עכשיו?",
        f"תזיז את '{title}' ל-07:30",
    ]
    try:
        for prompt in prompts:
            reply = turn(prompt, save=False, location=location, past=list(past))
            past += [{"role": "user", "text": prompt}, reply]
            report["turns"].append({"prompt": prompt, "reply": reply["text"], "actions": reply["actions"]})
        found = [e for e in calendar_agent.list_events(formatting.DEFAULT_TZ_NAME, 30, title, 1)
                 if e.get("summary") == title]
        report["events_in_calendar"] = [{"start": e["start"].get("dateTime")} for e in found]
        report["checks"] = {
            "exactly_one_event": len(found) == 1,
            "moved_to_0730": len(found) == 1 and found[0]["start"].get("dateTime", "")[11:16] == "07:30",
            "turn2_no_actions": not past[3]["actions"],
            "turn1_receipt": any("נוצר ביומן" in a for a in past[1]["actions"]),
            "turn3_receipt": any("עודכן ביומן" in a for a in past[5]["actions"]),
            "turn2_used_location": bool(location) and any(w in past[3]["text"] for w in ("°", "מעלות")),
            "turn3_receipt_once": past[5]["text"].count("✅") == 1,
        }
        # Calling create twice directly must give one event.
        # The event now sits at 07:30-07:45: creating it again directly must return it, not add one.
        again = calendar_agent.create_event(title, day, "07:30", "07:45")
        report["checks"]["create_is_idempotent"] = bool(found) and again["already_existed"] and again["id"] == found[0]["id"]
    finally:
        for e in calendar_agent.list_events(formatting.DEFAULT_TZ_NAME, 30, title, 1):
            if e.get("summary") == title:
                calendar_agent.delete_event(e["id"])
        report["cleanup_left"] = len([e for e in calendar_agent.list_events(formatting.DEFAULT_TZ_NAME, 30, title, 1)
                                      if e.get("summary") == title])
    report["ok"] = all(report.get("checks", {}).values()) and report["cleanup_left"] == 0
    return report
