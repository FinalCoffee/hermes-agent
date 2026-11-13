"""``telegram.accept_attachments: false``: a text-only bot refuses every inbound attachment.

A refused attachment is never fetched (no ``get_file``), cached or sent to a vision model, and
the replied-to media of a text reply is not re-downloaded either. The message still reaches the
agent: its text, then one note that an attachment arrived and this assistant does not read
attachments, so the model can tell the user. Group gating (``require_mention``) runs before this
module is reached, exactly as for an accepted attachment.
"""

from __future__ import annotations

from typing import Any

from gateway.platforms.event import MessageType
from plugins.platforms.telegram.telegram_media_fetch import TYPED_ATTR

ATTACHMENT_NOTE = "[Attachment not received: this assistant does not read attachments. Tell the user if it matters.]"
REPLIED_ATTACHMENT_NOTE = "[The replied-to message has an attachment, not received: this assistant does not read attachments.]"

_ATTACHMENT_ATTRS = ("photo", "video", "video_note", "audio", "voice", "document", "sticker", "animation")


def refused(adapter: Any) -> bool:
    """True unless ``telegram.accept_attachments`` is exactly ``true``: a value that is not a YAML
    boolean (a string, ``null``) refuses, so a typo never opens the bot to downloads."""
    extra = getattr(getattr(adapter, "config", None), "extra", None) or {}
    return extra.get("accept_attachments", True) is not True


def has_attachment(msg: Any) -> bool:
    return any(getattr(msg, attr, None) for attr in _ATTACHMENT_ATTRS)


async def dispatch_refused_media(adapter: Any, msg: Any, event: Any) -> bool:
    """A refused attachment message is dispatched here as text. True when it was handled (the caller stops).

    An album is one turn: its first item carries the note and the siblings join the album's debounce
    without one each (``str(group) in adapter._media_group_events`` is the pending album)."""
    if not refused(adapter) or not has_attachment(msg):
        return False
    event = adapter._apply_telegram_group_observe_attribution(event)
    event.message_type = MessageType.TEXT
    group = getattr(msg, "media_group_id", None)
    if group is None:
        event.text = adapter._append_observed_note(event.text, ATTACHMENT_NOTE)
        await adapter.handle_message(event)
        return True
    if str(group) not in adapter._media_group_events:
        event.text = adapter._append_observed_note(event.text, ATTACHMENT_NOTE)
    await adapter._queue_media_group_event(str(group), event)
    return True


def note_refused_observed(adapter: Any, msg: Any, event: Any) -> bool:
    """An unaddressed group attachment, observed as text: noted, never downloaded. True when refused."""
    if not refused(adapter) or not has_attachment(msg):
        return False
    event.text = adapter._append_observed_note(event.text, ATTACHMENT_NOTE)
    return True


def note_refused_replied(adapter: Any, msg: Any, event: Any) -> bool:
    """The media a text reply points at is not re-downloaded; the reply gets a note instead. True when refused."""
    reply_msg = getattr(msg, "reply_to_message", None)
    if not refused(adapter) or reply_msg is None or not has_attachment(reply_msg):
        return False
    # The typed text before any replied-to note, as cache_replied_media records it for voice clarifies.
    event.__dict__.setdefault(TYPED_ATTR, (event.text or "").strip())
    event.text = adapter._append_observed_note(event.text, REPLIED_ATTACHMENT_NOTE)
    return True
