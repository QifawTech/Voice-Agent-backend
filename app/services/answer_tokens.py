"""
Voice Agent Platform — Answer Tokens

The link in a reminder email has to work for someone who is not logged in and
never will be: an 82-year-old opening her mail. So the link itself has to carry
its own proof.

A token is an HMAC of (occurrence id, action) under the application secret.
That gives three properties, each of which matters:

    unguessable   without the secret, a valid token cannot be produced, so
                  nobody can close someone else's reminders by trying ids
    no state      nothing to store and nothing to expire-sweep; the id in the
                  URL is the whole record
    action-bound  the action is inside the signature, so a "mark it done" link
                  cannot be edited into a "snooze" link

What it deliberately is not: a login. A token closes one occurrence and can do
nothing else. It does not expire, because a reminder is answerable for as long
as it is open, and the occurrence's own terminal state is what stops a replay
from changing anything.
"""

import hmac
from base64 import urlsafe_b64encode
from hashlib import sha256
from uuid import UUID

from app.config import get_settings

# Length of the token in characters. A 20-byte HMAC prefix is 160 bits of
# unforgeability — far past brute force, and short enough not to wrap in a
# mail client.
_TOKEN_BYTES = 20

ACTIONS = ("done", "snooze")


def make_token(occurrence_id: UUID | str, action: str) -> str:
    """The token that proves a link to act on this occurrence is genuine."""
    if action not in ACTIONS:
        raise ValueError(f"Unknown answer action '{action}'")

    settings = get_settings()
    message = f"{occurrence_id}:{action}".encode()
    digest = hmac.new(
        settings.SECRET_KEY.encode(), message, sha256
    ).digest()[:_TOKEN_BYTES]
    return urlsafe_b64encode(digest).decode().rstrip("=")


def verify_token(occurrence_id: UUID | str, action: str, token: str) -> bool:
    """
    Whether a token matches this occurrence and action.

    compare_digest, not ==: a plain comparison returns as soon as two bytes
    differ, and the time it took leaks how much of the token was right.
    """
    if not token:
        return False
    try:
        expected = make_token(occurrence_id, action)
    except ValueError:
        return False
    return hmac.compare_digest(expected, token)


def answer_url(occurrence_id: UUID | str, action: str) -> str:
    """The full link to put in a message."""
    settings = get_settings()
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    token = make_token(occurrence_id, action)
    return f"{base}/api/v1/r/{occurrence_id}/{action}?t={token}"
