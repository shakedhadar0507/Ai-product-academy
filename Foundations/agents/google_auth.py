import os
import tempfile

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.modify",
]

WRITABLE_TOKEN_PATH = os.path.join(tempfile.gettempdir(), "token.json")


def get_credentials(scopes):
    if os.path.exists(WRITABLE_TOKEN_PATH):
        token_path = WRITABLE_TOKEN_PATH
    elif os.path.exists("/etc/secrets/token.json"):
        token_path = "/etc/secrets/token.json"
    else:
        token_path = "token.json"
    credentials_path = (
        "/etc/secrets/credentials.json" if os.path.exists("/etc/secrets/credentials.json") else "credentials.json"
    )

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, scopes)
            creds = flow.run_local_server(port=0)
        with open(WRITABLE_TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())
    return creds
