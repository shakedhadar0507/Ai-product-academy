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

import anthropic
from googleapiclient.discovery import build

from agents import action_agent, calendar_agent, formatting, garmin_agent, gmail_agent, google_auth, push_agent, uni_agent

MODEL = "claude-opus-5-5"
MAX_TURNS = 20
MAX_STEPS = 6

TOOLS = action_agent.TOOLS + [
    {
        "name": "get_calendar",
        "description": "Events on one day of the user's Google Calendar.",
        "input_schema": {"type": "object", "properties": {
            "days_ahead": {"type": "integer", "description": "0 = today, 1 = tomorrow, up to 14"},
        }, "required": ["days_ahead"]},
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
Use the tools to look things up instead of guessing, and to act when he asks. Mail is never sent:
you can only save Gmail drafts, so say "saved as a draft". Before creating events or reminders,
make sure the date and time are clear; if not, ask. Now is {now} ({weekday}), Israel time."""


def _gmail():
    return build("gmail", "v1", credentials=google_auth.get_credentials(google_auth.SCOPES))


def get_calendar(days_ahead):
    days = max(0, min(int(days_ahead), 14))
    events = calendar_agent.get_data(formatting.DEFAULT_TZ_NAME, days) or []
    return [{
        "time": e.get("start", {}).get("dateTime", "")[11:16] or "all day",
        "end": e.get("end", {}).get("dateTime", "")[11:16],
        "title": e.get("summary", ""),
        "location": e.get("location", ""),
    } for e in events]


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


def _tool_functions():
    tz = formatting.DEFAULT_TZ_NAME
    return {
        "create_calendar_event": lambda **kw: calendar_agent.create_event(tz_name=tz, **kw),
        "create_gmail_draft": lambda **kw: gmail_agent.create_draft(**kw),
        "get_calendar": get_calendar, "search_mail": search_mail, "read_mail": read_mail,
        "list_tasks": list_tasks, "add_reminder": add_reminder, "complete_task": complete_task,
        "get_training": get_training,
    }


def history():
    try:
        return (push_agent.get_config("chat") or {}).get("messages", [])
    except Exception:
        return []


def reset():
    push_agent.set_config("chat", {"messages": []})


def clean(text):
    return re.sub(r"[*#`]+", "", text or "").strip()


def ask(user_text, save=True):
    """One chat turn. Returns Claude's answer, and saves both sides to the history."""
    past = history()[-MAX_TURNS:]
    while past and past[0]["role"] != "user":
        past = past[1:]
    messages = [{"role": m["role"], "content": m["text"]} for m in past]
    messages.append({"role": "user", "content": user_text})
    now = formatting.now_in_tz()
    system = SYSTEM.format(now=now.strftime("%Y-%m-%d %H:%M"), weekday=now.strftime("%A"))
    client, functions, answer = anthropic.Anthropic(), _tool_functions(), ""
    for _ in range(MAX_STEPS):
        response = client.messages.create(
            model=MODEL, max_tokens=1500, system=system, tools=TOOLS, messages=messages,
            output_config={"effort": "low"},
        )
        answer = "\n".join(b.text for b in response.content if b.type == "text").strip() or answer
        calls = [b for b in response.content if b.type == "tool_use"]
        if not calls:
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for call in calls:
            try:
                output = functions[call.name](**call.input)
                content, is_error = json.dumps(output, ensure_ascii=False, default=str)[:8000], False
            except Exception as e:
                print(f"[ERROR] chat tool {call.name}: {type(e).__name__}: {e}", file=sys.stderr)
                content, is_error = f"Error: {type(e).__name__}: {str(e)[:200]}", True
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    answer = clean(answer) or "לא הצלחתי לענות. נסה לנסח אחרת."
    if not save:
        return answer
    saved = past + [{"role": "user", "text": user_text}, {"role": "assistant", "text": answer}]
    try:
        push_agent.set_config("chat", {"messages": saved[-MAX_TURNS:]})
    except Exception as e:
        print(f"[ERROR] chat save: {type(e).__name__}", file=sys.stderr)
    return answer
