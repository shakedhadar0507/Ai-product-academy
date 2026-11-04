"""Gmail filters that keep the inbox clean, applied by the server (no Gmail UI steps).

Each rule is a Gmail filter plus a one-time clean-up of mail that already matches it.
apply_rules() is idempotent: it creates a filter only if an identical one is missing,
so it is safe to call on every scheduled check.
"""

from googleapiclient.discovery import build

from agents import google_auth

INVITES_LABEL = "הזמנות יומן"

# Calendar invitations and RSVP replies all carry an .ics file. The event still
# lands in Google Calendar; only the email skips the inbox.
RULES = [
    {
        "name": "calendar-invites",
        "criteria": {"query": "filename:ics"},
        "add_label": INVITES_LABEL,
        "remove": ["INBOX"],
        "cleanup_query": "in:inbox filename:ics",
    },
    # Blocked sender. "TRASH" is Gmail's "Delete it" filter action (recoverable for 30 days).
    {
        "name": "blocked-nitai",
        "criteria": {"from": "nitai@americanlands.co.il"},
        "add_label": "TRASH",
        "remove": ["INBOX"],
        "cleanup_query": "from:nitai@americanlands.co.il -in:trash",
    },
]


def _label_id(service, name):
    if name in ("TRASH", "SPAM", "STARRED", "IMPORTANT"):
        return name
    for label in service.users().labels().list(userId="me").execute().get("labels", []):
        if label["name"] == name:
            return label["id"]
    created = service.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def _ensure_filter(service, criteria, action):
    filters = service.users().settings().filters().list(userId="me").execute().get("filter", [])
    for existing in filters:
        if existing.get("criteria") == criteria:
            current = existing.get("action", {})
            if set(current.get("addLabelIds", [])) >= set(action["addLabelIds"]) and set(
                current.get("removeLabelIds", [])
            ) >= set(action["removeLabelIds"]):
                return "exists"
            # Filter actions can't be patched, so replace it.
            service.users().settings().filters().delete(userId="me", id=existing["id"]).execute()
            service.users().settings().filters().create(
                userId="me", body={"criteria": criteria, "action": action}
            ).execute()
            return "replaced"
    service.users().settings().filters().create(
        userId="me", body={"criteria": criteria, "action": action}
    ).execute()
    return "created"


def _apply_to_existing(service, query, action):
    ids, page_token = [], None
    while True:
        response = service.users().messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=500
        ).execute()
        ids.extend(m["id"] for m in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    for start in range(0, len(ids), 1000):
        service.users().messages().batchModify(
            userId="me",
            body={
                "ids": ids[start:start + 1000],
                "addLabelIds": action["addLabelIds"],
                "removeLabelIds": action["removeLabelIds"],
            },
        ).execute()
    return len(ids)


def apply_rules():
    service = build("gmail", "v1", credentials=google_auth.get_credentials(google_auth.SCOPES))
    results = {}
    for rule in RULES:
        try:
            action = {"addLabelIds": [_label_id(service, rule["add_label"])], "removeLabelIds": rule["remove"]}
            status = _ensure_filter(service, rule["criteria"], action)
            moved = _apply_to_existing(service, rule["cleanup_query"], action)
            results[rule["name"]] = {"filter": status, "moved": moved}
        except Exception as e:
            results[rule["name"]] = {"error": f"{type(e).__name__}: {e}"[:300]}
    return results
