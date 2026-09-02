import base64
import json
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

sys.stdout.reconfigure(encoding="utf-8")

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def get_credentials():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token_file:
            token_file.write(creds.to_json())
    return creds


def get_header(headers, name):
    for header in headers:
        if header["name"].lower() == name.lower():
            return header["value"]
    return "(unknown)"


def get_unread_emails(creds, max_results=5):
    service = build("gmail", "v1", credentials=creds)

    results = service.users().messages().list(
        userId="me",
        labelIds=["UNREAD", "INBOX"],
        maxResults=max_results,
    ).execute()

    message_refs = results.get("messages", [])

    emails = []
    for ref in message_refs:
        message = service.users().messages().get(
            userId="me",
            id=ref["id"],
            format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        emails.append(message)

    return emails


def main():
    creds = get_credentials()

    try:
        emails = get_unread_emails(creds)

        if not emails:
            print("  No unread emails found.")
        else:
            print("\nRaw API response (first email):")
            print(json.dumps(emails[0], indent=2))

            print("\nUnread emails:")
            for email in emails:
                headers = email["payload"]["headers"]
                sender = get_header(headers, "From")
                subject = get_header(headers, "Subject")
                print(f"  From: {sender} | Subject: {subject}")
    except Exception:
        print("Could not fetch unread emails")


if __name__ == "__main__":
    main()
