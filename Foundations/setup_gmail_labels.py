"""One-time setup: create Gmail labels + filters and retroactively label matching mail.

Every filter here is expected to skip the inbox (removeLabelIds: ["INBOX"]) in addition to
adding its label, so matching mail lives only under its label and never also sits in the
primary Inbox. This is enforced both for the filter definition (going forward) and
retroactively for existing messages that already carry the label but are still in the Inbox.
"""

import sys

from googleapiclient.discovery import build

from agents import google_auth

sys.stdout.reconfigure(encoding="utf-8")

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
    {
        "label": "לא רלוונטי",
        "criteria": {"query": "category:social OR category:updates OR category:forums OR category:promotions OR in:spam"},
        "query": "category:social OR category:updates OR category:forums OR category:promotions OR in:spam",
    },
]

VISIBLE_LABELS = ["עבודה", "קבלות", "לא רלוונטי"]

# Business/newsletter senders to add to "לא רלוונטי" on top of the category-based filter above.
# Resolved from real message From headers (not display names, which aren't reliable for
# filtering). Personal contacts (Maya Ben Sadoun, Maya Zaks, Shay Thieberg, Claude Team) are
# deliberately excluded from this list -- they're real people, not businesses -- except where
# LinkedIn itself is the sending address (see note below).
IRRELEVANT_BUSINESS_SENDERS = {
    "Etsy": ["email@email.etsy.com"],
    # LinkedIn's automated addresses. invitations@/messages-noreply@/messaging-digest-noreply@
    # also carry connection requests and DMs from real people (including Maya Zaks and Shay
    # Thieberg) since LinkedIn always sends from its own domain regardless of who initiated the
    # message -- included anyway per explicit user confirmation that all LinkedIn automated
    # mail should be treated as irrelevant.
    "LinkedIn": [
        "newsletters-noreply@linkedin.com",
        "notifications-noreply@linkedin.com",
        "updates-noreply@linkedin.com",
        "invitations@linkedin.com",
        "messages-noreply@linkedin.com",
        "messaging-digest-noreply@linkedin.com",
    ],
    # "Must Reads" is the same Seeking Alpha brand under a different campaign name; merged here.
    "Seeking Alpha / Must Reads": [
        "account@seekingalpha.com",
        "subscriptions@seekingalpha.com",
        "seekingalpha@mail.sailthru.com",
    ],
    "KSP.co.il": [
        "today@kspmail.co.il",
        "no-reply@ksp.co.il",
        "noreply@ksp.co.il",
    ],
    "myIQ": ["no-reply@email.myiq.com"],
    "SmartyMe": ["info@smartymeapp.com"],
    "Google Accounts Team": [
        "no-reply@accounts.google.com",
        "noreply-accounts@google.com",
    ],
}


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


def ensure_filter_skips_inbox(service, criteria, label_id):
    """Create the filter if missing. If it exists but doesn't skip the inbox, delete and
    recreate it (filter actions can't be patched in place)."""
    filters = service.users().settings().filters().list(userId="me").execute().get("filter", [])
    for existing in filters:
        action = existing.get("action", {})
        if existing.get("criteria") == criteria and label_id in action.get("addLabelIds", []):
            if "INBOX" in action.get("removeLabelIds", []):
                return existing["id"], "already existed"
            service.users().settings().filters().delete(userId="me", id=existing["id"]).execute()
            created = service.users().settings().filters().create(
                userId="me",
                body={
                    "criteria": criteria,
                    "action": {"addLabelIds": [label_id], "removeLabelIds": ["INBOX"]},
                },
            ).execute()
            return created["id"], "fixed (was missing removeLabelIds INBOX)"
    created = service.users().settings().filters().create(
        userId="me",
        body={
            "criteria": criteria,
            "action": {"addLabelIds": [label_id], "removeLabelIds": ["INBOX"]},
        },
    ).execute()
    return created["id"], "created"


def verify_filter_skips_inbox(service, filter_id):
    """Read the filter back from the API to confirm it actually skips the inbox."""
    existing = service.users().settings().filters().get(userId="me", id=filter_id).execute()
    return "INBOX" in existing.get("action", {}).get("removeLabelIds", [])


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


def get_message_ids(service, query):
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
    return message_ids


def remove_inbox_for_query(service, query):
    """Remove INBOX from every existing message matching `query` that's still in the Inbox.
    Keeps any labels intact -- equivalent to archiving, not deleting."""
    message_ids = get_message_ids(service, f"{query} in:inbox")
    for start in range(0, len(message_ids), 1000):
        chunk = message_ids[start:start + 1000]
        service.users().messages().batchModify(
            userId="me", body={"ids": chunk, "removeLabelIds": ["INBOX"]}
        ).execute()
    return len(message_ids)


def archive_labeled_messages(service, label_name):
    return remove_inbox_for_query(service, f'label:"{label_name}"')


def set_label_visibility(service, name, visibility="labelShow"):
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    label = next((label for label in labels if label["name"] == name), None)
    if not label:
        print(f"{name}: label not found, skipping visibility update")
        return
    service.users().labels().patch(
        userId="me", id=label["id"], body={"labelListVisibility": visibility}
    ).execute()
    print(f"{name}: labelListVisibility set to {visibility}")


def main():
    creds = google_auth.get_credentials(google_auth.SCOPES)
    service = build("gmail", "v1", credentials=creds)

    for definition in LABEL_DEFINITIONS:
        label_id = get_or_create_label(service, definition["label"])
        filter_id, filter_status = ensure_filter_skips_inbox(service, definition["criteria"], label_id)
        assert verify_filter_skips_inbox(service, filter_id), (
            f"{definition['label']}: filter {filter_id} does not skip inbox after update"
        )
        labeled_count = label_existing_messages(service, definition["query"], label_id)
        archived_count = archive_labeled_messages(service, definition["label"])
        print(
            f"{definition['label']}: filter {filter_status} (verified skip-inbox), "
            f"{labeled_count} existing emails labeled, "
            f"{archived_count} messages removed from Inbox"
        )

    for label_name in VISIBLE_LABELS:
        set_label_visibility(service, label_name)

    print()
    print("Resolved business/newsletter sender -> email mapping:")
    for name, emails in IRRELEVANT_BUSINESS_SENDERS.items():
        print(f"  {name}: {', '.join(emails)}")

    irrelevant_label_id = get_or_create_label(service, "לא רלוונטי")
    all_emails = sorted({email for emails in IRRELEVANT_BUSINESS_SENDERS.values() for email in emails})
    business_criteria = {"from": " OR ".join(all_emails)}
    filter_id, filter_status = ensure_filter_skips_inbox(service, business_criteria, irrelevant_label_id)
    assert verify_filter_skips_inbox(service, filter_id), (
        f"business-sender filter {filter_id} does not skip inbox after update"
    )

    from_query = "from:(" + " OR ".join(all_emails) + ")"
    labeled_count = label_existing_messages(service, from_query, irrelevant_label_id)
    archived_count = remove_inbox_for_query(service, from_query)
    print(
        f"\nBusiness-sender filter {filter_status} (id={filter_id}, verified skip-inbox): "
        f"{labeled_count} existing messages labeled 'לא רלוונטי', "
        f"{archived_count} removed from Inbox"
    )


if __name__ == "__main__":
    main()
