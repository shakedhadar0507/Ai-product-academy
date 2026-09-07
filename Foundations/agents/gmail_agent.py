import base64
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
