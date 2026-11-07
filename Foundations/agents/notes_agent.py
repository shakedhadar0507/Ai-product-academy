"""Reminders from the iPhone Notes app.

Apple Notes has no API a server can call, so an iPhone Shortcut automation runs whenever the Notes
app is closed and POSTs the recently edited notes to /notes/in/<token> (the token is made here and
kept in the Supabase settings row "config:notes"; the setup page /notes/setup shows the steps).

Claude reads each new or changed note once and pulls out anything with a date or time
("dentist Tuesday 15:00", "send the report by Thursday"). Each one becomes a task in uni_tasks
(source "notes"), so it shows in the tasks card and gets a push at its time from the regular check.
Note text is not stored: only a hash of each note, so the same note is not read twice.
"""
import hashlib
import hmac
import json
import secrets
import sys
import threading
from datetime import datetime, timezone

import anthropic

from agents import formatting, push_agent, uni_agent

NOTE_MARK = "@@NOTE@@"
MAX_NOTES = 30
MAX_NOTE_CHARS = 4000
DEFAULT_HOUR = 9

_state = {"last": None}


def token():
    stored = push_agent.get_config("notes") or {}
    if not stored.get("token"):
        stored = {"token": secrets.token_urlsafe(24)}
        push_agent.set_config("notes", stored)
    return stored["token"]


def token_ok(candidate):
    stored = (push_agent.get_config("notes") or {}).get("token")
    return bool(stored and candidate) and hmac.compare_digest(stored, candidate)


def split_notes(text):
    """The Shortcut starts every note with NOTE_MARK. Plain text without marks is one note."""
    parts = text.split(NOTE_MARK) if NOTE_MARK in text else [text]
    notes = [p.strip()[:MAX_NOTE_CHARS] for p in parts if p.strip()]
    return notes[:MAX_NOTES]


def _note_key(note):
    return "note:" + hashlib.sha256(" ".join(note.split()).encode()).hexdigest()[:20]


SCHEMA = {
    "type": "object",
    "properties": {
        "reminders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "when": {"type": "string"}},
                "required": ["title", "when"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reminders"],
    "additionalProperties": False,
}

PROMPT = """These are notes a student wrote in his phone's Notes app. Find every item he needs to be
reminded of at a specific time: appointments, meetings, deadlines, things to do on a given day.

Now is {now} (Israel time, {weekday}).
For each item give:
"title": in the note's language, short and clear, max 60 characters (what to do or where to be).
"when": YYYY-MM-DDTHH:MM in Israel time. Resolve words like "tomorrow" or "Tuesday" from now and
from the note's last-edited date when it is given. With a day but no hour use {default_hour:02d}:00.
For a deadline, use the deadline itself.

Skip items with no date at all, items already in the past, and anything already in this list of
existing reminders (even if worded differently):
{existing}

Notes:
{notes}"""


def extract(notes, existing):
    now = formatting.now_in_tz()
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-5-5",
        max_tokens=2048,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": PROMPT.format(
            now=now.strftime("%Y-%m-%d %H:%M"), weekday=now.strftime("%A"), default_hour=DEFAULT_HOUR,
            existing="\n".join(f"- {t}" for t in existing) or "(none)",
            notes="\n\n---\n\n".join(notes),
        )}],
    )
    return json.loads(next(b.text for b in response.content if b.type == "text"))["reminders"]


def _parse_when(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=formatting.ISRAEL_TZ) if parsed.tzinfo is None else parsed


def to_tasks(reminders):
    now = datetime.now(timezone.utc)
    tasks = []
    for item in reminders:
        due = _parse_when(item.get("when"))
        title = (item.get("title") or "").strip()[:80]
        if not title or not due or due <= now:
            continue
        key = "notes:" + hashlib.sha256(f"{title}|{due.isoformat()}".encode()).hexdigest()[:20]
        tasks.append({"key": key, "source": "notes", "kind": "note", "title": title,
                      "course": None, "due": due.isoformat(), "url": None})
    return tasks


def ingest(text, dry_run=False):
    """Read the notes that changed since last time. Returns a small result dict."""
    notes = split_notes(text or "")
    keyed = {_note_key(n): n for n in notes}
    seen = set() if dry_run else uni_agent._already_sent(list(keyed))
    fresh = [n for k, n in keyed.items() if k not in seen]
    result = {"notes": len(notes), "new_notes": len(fresh), "reminders": 0}
    if fresh:
        existing = [] if dry_run else [
            f"{t['title']} ({t['due'].astimezone(formatting.ISRAEL_TZ):%Y-%m-%d %H:%M})"
            for t in uni_agent.list_tasks() if t.get("source") == "notes" and not t["done"] and t["due"]
        ]
        tasks = to_tasks(extract(fresh, existing))
        result["reminders"] = len(tasks)
        if dry_run:
            result["found"] = [{"title": t["title"], "due": t["due"]} for t in tasks]
            return result
        uni_agent._save_tasks(tasks)
        if tasks:
            lines = " · ".join(f"{t['title']} ({uni_agent.format_due(datetime.fromisoformat(t['due']))})" for t in tasks[:3])
            title = "📝 תזכורת חדשה מהפתקים" if len(tasks) == 1 else f"📝 {len(tasks)} תזכורות חדשות מהפתקים"
            push_agent.send_to_all(title, lines[:170], uni_agent.DASHBOARD_URL)
    if not dry_run:
        uni_agent._mark_sent([k for k in keyed if k not in seen])
    return result


def ingest_in_background(text):
    def work():
        try:
            result = ingest(text)
            result["ok"] = True
        except Exception as e:
            print(f"[ERROR] notes ingest: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            result = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        result["at"] = formatting.now_in_tz().strftime("%d/%m %H:%M")
        _state["last"] = result
        try:
            push_agent.set_config("notes_last", result)
        except Exception:
            pass

    threading.Thread(target=work, daemon=True).start()


def status():
    last = _state["last"]
    if last is None:
        try:
            last = push_agent.get_config("notes_last")
        except Exception:
            last = None
    return {"last": last}
