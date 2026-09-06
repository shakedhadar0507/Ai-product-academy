from datetime import datetime


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
