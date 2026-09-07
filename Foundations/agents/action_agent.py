import sys

import anthropic
from dotenv import load_dotenv

from agents import calendar_agent, formatting, gmail_agent

load_dotenv()

TOOLS = [
    {
        "name": "create_calendar_event",
        "description": "Create a new event on the user's Google Calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "date": {"type": "string", "description": "Event date, in YYYY-MM-DD format"},
                "start_time": {"type": "string", "description": "Start time, in 24-hour HH:MM format"},
                "end_time": {"type": "string", "description": "End time, in 24-hour HH:MM format"},
                "description": {"type": "string", "description": "Optional event description"},
                "color": {
                    "type": "string",
                    "description": (
                        "Optional color for the event, as a plain color name (e.g. 'red', 'blue', "
                        "'green', 'purple', 'yellow', 'orange', 'pink', 'gray'). Only include this "
                        "if the user explicitly asks for a specific color."
                    ),
                },
                "recurrence": {
                    "type": "string",
                    "description": (
                        "Optional recurrence rule, as a full RRULE string (e.g. "
                        "'RRULE:FREQ=WEEKLY;BYDAY=MO' for 'every Monday', 'RRULE:FREQ=DAILY' for "
                        "'every day', 'RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR' for 'every Monday, "
                        "Wednesday and Friday'). Translate the user's plain-language recurrence "
                        "request into a valid RRULE string yourself. Only include this if the user "
                        "explicitly asks for a repeating event."
                    ),
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of email addresses to invite to the event. Google Calendar "
                        "automatically sends each of them a real invitation email — only include "
                        "this if the user explicitly asks to invite someone by their email address."
                    ),
                },
            },
            "required": ["summary", "date", "start_time", "end_time"],
        },
    },
    {
        "name": "create_gmail_draft",
        "description": "Create a Gmail draft email. This only saves a draft — it never sends anything.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Email body text"},
            },
            "required": ["to", "subject", "body"],
        },
    },
]

SYSTEM_PROMPT = (
    "You help manage a personal calendar and inbox by calling the tools available "
    "to you. If the user's message contains multiple distinct requests, call the "
    "relevant tool once per distinct request — even if it is the same tool called "
    "multiple times (for example, two separate calendar events, or an event plus a "
    "draft). Do not merge multiple requests into a single tool call, and do not "
    "skip any of them."
)


def _build_tool_functions(tz_name):
    return {
        "create_calendar_event": lambda **kwargs: calendar_agent.create_event(tz_name=tz_name, **kwargs),
        "create_gmail_draft": lambda **kwargs: gmail_agent.create_draft(**kwargs),
    }


def handle_request(user_text, tz_name=formatting.DEFAULT_TZ_NAME):
    tool_functions = _build_tool_functions(tz_name)
    try:
        client = anthropic.Anthropic()
        today_str = formatting.now_in_tz(tz_name).strftime("%Y-%m-%d (%A)")
        prompt = f"Today's date is {today_str}, in the {tz_name} timezone.\n\n{user_text}"
        messages = [{"role": "user", "content": prompt}]

        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
        if not tool_use_blocks:
            text_block = next((block for block in response.content if block.type == "text"), None)
            return text_block.text if text_block else "לא הצלחתי להבין איזו פעולה לבצע."

        tool_results = []
        for tool_use_block in tool_use_blocks:
            try:
                result = tool_functions[tool_use_block.name](**tool_use_block.input)
                tool_result_content = f"Success: {result}"
                is_error = False
            except Exception as e:
                print(f"[ERROR] action_agent tool execution ({tool_use_block.name}): {e}", file=sys.stderr)
                tool_result_content = f"Error: {e}"
                is_error = True
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_block.id,
                "content": tool_result_content,
                "is_error": is_error,
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        follow_up = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        final_text = next((block.text for block in follow_up.content if block.type == "text"), None)
        fallback_summary = "; ".join(result["content"] for result in tool_results)
        return final_text or fallback_summary
    except Exception as e:
        print(f"[ERROR] action_agent: {e}", file=sys.stderr)
        return "משהו השתבש בביצוע הפעולה."
