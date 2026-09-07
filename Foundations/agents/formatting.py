from datetime import datetime
from zoneinfo import ZoneInfo

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def now_in_israel():
    """Return the current datetime in Asia/Jerusalem, regardless of the server's own timezone."""
    return datetime.now(ISRAEL_TZ)


def today_in_israel():
    """Return today's date in Asia/Jerusalem, regardless of the server's own timezone."""
    return now_in_israel().date()


def format_date(value):
    """Format a date (date/datetime object, or 'YYYY-MM-DD' ISO string) as DD/MM."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.strftime("%d/%m")


def format_time_range(start_iso, end_iso):
    """Format two ISO datetime strings as 'HH:MM–HH:MM', or 'All day' if either is missing."""
    if not start_iso or not end_iso:
        return "All day"
    start_time = datetime.fromisoformat(start_iso).strftime("%H:%M")
    end_time = datetime.fromisoformat(end_iso).strftime("%H:%M")
    return f"{start_time}–{end_time}"
