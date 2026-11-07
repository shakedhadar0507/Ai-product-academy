"""Morning and evening digest pushes.

07:00: today's calendar, tasks due today or tomorrow, last night's sleep and readiness.
21:00: tomorrow's calendar, tasks due tomorrow, and anything still open from today.
Claude writes one short Hebrew push from that; if Claude fails, a plain template is sent.
The day each digest was last sent is kept in the settings row "config:digest", so a restart
never sends it twice, and a digest missed by more than an hour (server down) is skipped.
"""
import sys
from datetime import timedelta

import anthropic

from agents import calendar_agent, formatting, garmin_agent, push_agent, uni_agent

SLOTS = {"morning": 7, "evening": 21}
LATE_LIMIT_MIN = 60


def _events(days_ahead):
    try:
        events = calendar_agent.get_data(formatting.DEFAULT_TZ_NAME, days_ahead) or []
    except Exception as e:
        print(f"[WARN] digest calendar: {type(e).__name__}", file=sys.stderr)
        return []
    lines = []
    for event in events:
        start = event.get("start", {})
        when = start.get("dateTime", "")[11:16] or "כל היום"
        lines.append(f"{when} {event.get('summary', '')}")
    return lines


def _tasks(until):
    try:
        tasks = uni_agent.list_tasks()
    except Exception as e:
        print(f"[WARN] digest tasks: {type(e).__name__}", file=sys.stderr)
        return []
    now = formatting.now_in_tz()
    return [
        f"{t['title']} ({uni_agent.format_due(t['due'])}{' · עבר' if t['due'] < now else ''})"
        for t in tasks if not t["done"] and t["due"] and t["due"] <= until
    ]


def _body_facts():
    if not garmin_agent.is_configured():
        return ""
    try:
        wellness = garmin_agent.get_data(formatting.today_in_tz())["wellness"] or {}
    except Exception:
        return ""
    parts = []
    if wellness.get("sleep_s"):
        parts.append(f"sleep {wellness['sleep_s'] / 3600:.1f}h")
    if wellness.get("training_readiness"):
        parts.append(f"training readiness {wellness['training_readiness']}")
    return ", ".join(parts)


def gather(slot):
    now = formatting.now_in_tz()
    end_of_tomorrow = (now + timedelta(days=1)).replace(hour=23, minute=59)
    if slot == "morning":
        return {"calendar": _events(0), "tasks": _tasks(end_of_tomorrow), "body": _body_facts()}
    return {"calendar": _events(1), "tasks": _tasks(end_of_tomorrow), "body": ""}


PROMPT = """Write a phone notification for a student, in Hebrew, max 170 characters, plain text, no
emoji, no title. It is his {slot} summary: {focus}. Name the first event and its time, and the most
urgent task. If there is nothing, say so briefly.

Calendar: {calendar}
Tasks: {tasks}
Body: {body}"""


def compose(slot, facts):
    focus = ("what today holds" if slot == "morning"
             else "what tomorrow holds and what is still open")
    try:
        response = anthropic.Anthropic().messages.create(
            model="claude-opus-5-5",
            max_tokens=400,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": PROMPT.format(
                slot=slot, focus=focus, calendar="; ".join(facts["calendar"]) or "nothing",
                tasks="; ".join(facts["tasks"]) or "none", body=facts["body"] or "unknown",
            )}],
        )
        text = next(b.text for b in response.content if b.type == "text").strip()
        if text:
            return text[:178]
    except Exception as e:
        print(f"[WARN] digest compose: {type(e).__name__}", file=sys.stderr)
    parts = []
    if facts["calendar"]:
        parts.append(f"{len(facts['calendar'])} אירועים, ראשון: {facts['calendar'][0]}")
    if facts["tasks"]:
        parts.append(f"משימות: {facts['tasks'][0]}")
    return " · ".join(parts)[:178] or "אין אירועים ומשימות פתוחות."


def send(slot):
    facts = gather(slot)
    body = compose(slot, facts)
    title = "☀️ סיכום בוקר" if slot == "morning" else "🌙 סיכום ערב"
    sent = push_agent.send_to_all(title, body, "/")
    return {"slot": slot, "devices": sent, "body": body}


def due_slot(now=None):
    """The digest that should go out now and has not gone out today, or None."""
    now = now or formatting.now_in_tz()
    try:
        state = push_agent.get_config("digest") or {}
    except Exception:
        return None
    today = now.date().isoformat()
    for slot, hour in SLOTS.items():
        start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if start <= now < start + timedelta(minutes=LATE_LIMIT_MIN) and state.get(slot) != today:
            return slot
    return None


def run_if_due():
    """Called every minute by the background loop. Returns the result when one was sent."""
    slot = due_slot()
    if not slot:
        return None
    state = push_agent.get_config("digest") or {}
    state[slot] = formatting.now_in_tz().date().isoformat()
    push_agent.set_config("digest", state)  # mark first, so a failure never repeats it
    result = send(slot)
    state["last"] = {**result, "at": formatting.now_in_tz().strftime("%d/%m %H:%M")}
    push_agent.set_config("digest", state)
    return result
