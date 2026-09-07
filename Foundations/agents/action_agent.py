import sys

import anthropic
from dotenv import load_dotenv

from agents import calendar_agent, gmail_agent

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

TOOL_FUNCTIONS = {
    "create_calendar_event": lambda **kwargs: calendar_agent.create_event(**kwargs),
    "create_gmail_draft": lambda **kwargs: gmail_agent.create_draft(**kwargs),
}


def handle_request(user_text):
    try:
        client = anthropic.Anthropic()
        messages = [{"role": "user", "content": user_text}]

        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=500,
            tools=TOOLS,
            messages=messages,
        )

        tool_use_block = next((block for block in response.content if block.type == "tool_use"), None)
        if not tool_use_block:
            text_block = next((block for block in response.content if block.type == "text"), None)
            return text_block.text if text_block else "לא הצלחתי להבין איזו פעולה לבצע."

        try:
            result = TOOL_FUNCTIONS[tool_use_block.name](**tool_use_block.input)
            tool_result_content = f"Success: {result}"
            is_error = False
        except Exception as e:
            print(f"[ERROR] action_agent tool execution ({tool_use_block.name}): {e}", file=sys.stderr)
            tool_result_content = f"Error: {e}"
            is_error = True

        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_block.id,
                "content": tool_result_content,
                "is_error": is_error,
            }],
        })

        follow_up = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=300,
            tools=TOOLS,
            messages=messages,
        )
        final_text = next((block.text for block in follow_up.content if block.type == "text"), None)
        return final_text or tool_result_content
    except Exception as e:
        print(f"[ERROR] action_agent: {e}", file=sys.stderr)
        return "משהו השתבש בביצוע הפעולה."
