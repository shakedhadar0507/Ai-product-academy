from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_TZ_NAME = "Asia/Jerusalem"
ISRAEL_TZ = ZoneInfo(DEFAULT_TZ_NAME)


def resolve_tz_name(tz_name):
    """Return tz_name if it's a valid IANA zone, otherwise fall back to the default timezone."""
    try:
        ZoneInfo(tz_name)
        return tz_name
    except Exception:
        return DEFAULT_TZ_NAME


def now_in_tz(tz_name=DEFAULT_TZ_NAME):
    """Return the current datetime in the given IANA timezone, regardless of the server's own timezone."""
    return datetime.now(ZoneInfo(tz_name))


def today_in_tz(tz_name=DEFAULT_TZ_NAME):
    """Return today's date in the given IANA timezone, regardless of the server's own timezone."""
    return now_in_tz(tz_name).date()


def now_in_israel():
    """Backward-compatible wrapper: now_in_tz() defaulted to Asia/Jerusalem."""
    return now_in_tz(DEFAULT_TZ_NAME)


def today_in_israel():
    """Backward-compatible wrapper: today_in_tz() defaulted to Asia/Jerusalem."""
    return today_in_tz(DEFAULT_TZ_NAME)


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
