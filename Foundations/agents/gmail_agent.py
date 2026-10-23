import base64
import html
import re
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from agents import google_auth


def create_draft(to, subject, body):
    creds = google_auth.get_credentials(google_auth.SCOPES)
    service = build("gmail", "v1", credentials=creds)

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    draft = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return {
        "id": draft.get("id"),
        "to": to,
        "subject": subject,
    }


def _decode_part(part):
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data)
    charset = "utf-8"
    for header in part.get("headers", []):
        match = re.search(r'charset="?([\w-]+)', header["value"]) if header["name"].lower() == "content-type" else None
        if match:
            charset = match.group(1).lower()
    # Uni mails use iso-8859-8-i, which Python only knows as iso-8859-8.
    charset = {"iso-8859-8-i": "iso-8859-8", "windows-1255": "cp1255"}.get(charset, charset)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def mail_text(payload):
    """Readable text of a Gmail message payload: the plain part, or the HTML part stripped of tags."""
    plain, rich = [], []
    stack = [payload]
    while stack:
        part = stack.pop()
        stack.extend(part.get("parts", []))
        if part.get("mimeType") == "text/plain":
            plain.append(_decode_part(part))
        elif part.get("mimeType") == "text/html":
            rich.append(_decode_part(part))
    if plain:
        return "\n".join(plain)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", "\n".join(rich), flags=re.S | re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


GMAIL_WEB_URL = "https://mail.google.com/mail/u/0/#all/{}"


def get_message(message_id):
    service = build("gmail", "v1", credentials=google_auth.get_credentials(google_auth.SCOPES))
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in message["payload"].get("headers", [])}
    return {
        "id": message_id,
        "thread_id": message["threadId"],
        "from": headers.get("from", ""),
        "reply_to": headers.get("reply-to") or headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "message_id": headers.get("message-id", ""),
        "references": headers.get("references", ""),
        "text": mail_text(message["payload"]) or message.get("snippet", ""),
        "web_url": GMAIL_WEB_URL.format(message_id),
    }


def reply(message_id, body, send):
    """Reply in the same thread. send=False only saves a Gmail draft."""
    original = get_message(message_id)
    subject = original["subject"]
    message = MIMEText(body, _charset="utf-8")
    message["to"] = original["reply_to"]
    message["subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if original["message_id"]:
        message["In-Reply-To"] = original["message_id"]
        message["References"] = (original["references"] + " " + original["message_id"]).strip()
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    payload = {"raw": raw, "threadId": original["thread_id"]}

    service = build("gmail", "v1", credentials=google_auth.get_credentials(google_auth.SCOPES))
    if send:
        service.users().messages().send(userId="me", body=payload).execute()
    else:
        service.users().drafts().create(userId="me", body={"message": payload}).execute()
    return original["reply_to"]
