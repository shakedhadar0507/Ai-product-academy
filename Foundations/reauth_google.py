"""Re-authorize Google (Calendar + Gmail) and write a fresh token.json.

Run on your own computer from the Foundations folder when Render logs show
'invalid_grant': python reauth_google.py
Then upload the new token.json to Render > Environment > Secret Files.
"""
from google_auth_oauthlib.flow import InstalledAppFlow

from agents.google_auth import SCOPES

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
with open("token.json", "w") as token_file:
    token_file.write(creds.to_json())
print("token.json updated" + ("" if creds.refresh_token else " (WARNING: no refresh token)"))
