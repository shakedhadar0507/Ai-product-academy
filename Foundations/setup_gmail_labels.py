"""One-time setup: create Gmail labels + filters and retroactively label matching mail."""

from googleapiclient.discovery import build

from agents import google_auth

LABEL_DEFINITIONS = [
    {
        "label": "לימודים",
        "criteria": {"to": "shaked.hadar@post.runi.ac.il"},
        "query": "to:shaked.hadar@post.runi.ac.il",
    },
    {
        "label": "RFS",
        "criteria": {"from": "yonatan.bunzel@gmail.com OR adamteer0503@gmail.com"},
        "query": "from:(yonatan.bunzel@gmail.com OR adamteer0503@gmail.com)",
    },
    {
        "label": "פיננסים",
        "criteria": {"from": "HarelInsurance@harel-group.co.il"},
        "query": "from:HarelInsurance@harel-group.co.il",
    },
]


def get_or_create_label(service, name):
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"] == name:
            return label["id"]
    created = service.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def get_or_create_filter(service, criteria, label_id):
    filters = service.users().settings().filters().list(userId="me").execute().get("filter", [])
    for existing in filters:
        action = existing.get("action", {})
        if existing.get("criteria") == criteria and label_id in action.get("addLabelIds", []):
            return existing["id"], False
    created = service.users().settings().filters().create(
        userId="me",
        body={
            "criteria": criteria,
            "action": {"addLabelIds": [label_id], "removeLabelIds": ["INBOX"]},
        },
    ).execute()
    return created["id"], True


def label_existing_messages(service, query, label_id):
    message_ids = []
    page_token = None
    while True:
        response = service.users().messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=500
        ).execute()
        message_ids.extend(message["id"] for message in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    for start in range(0, len(message_ids), 1000):
        chunk = message_ids[start:start + 1000]
        service.users().messages().batchModify(
            userId="me", body={"ids": chunk, "addLabelIds": [label_id]}
        ).execute()

    return len(message_ids)


def main():
    creds = google_auth.get_credentials(google_auth.SCOPES)
    service = build("gmail", "v1", credentials=creds)

    for definition in LABEL_DEFINITIONS:
        label_id = get_or_create_label(service, definition["label"])
        _, filter_created = get_or_create_filter(service, definition["criteria"], label_id)
        labeled_count = label_existing_messages(service, definition["query"], label_id)
        filter_status = "created" if filter_created else "already existed"
        print(f"{definition['label']}: filter {filter_status}, {labeled_count} existing emails labeled")


if __name__ == "__main__":
    main()
