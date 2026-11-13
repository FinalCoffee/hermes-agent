"""The last-resort "this turn failed" notice an adapter sends when a turn raised.

Moved out of ``base.py`` to keep that facade under its size ratchet; ``BasePlatformAdapter._notify_turn_error``
delegates here and session ingress still calls the adapter method.
"""

import logging
from typing import TYPE_CHECKING, Optional

from agent.i18n import t

if TYPE_CHECKING:
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.platforms.event import MessageEvent

logger = logging.getLogger("gateway.platforms.base")


async def notify_turn_error(
    adapter: "BasePlatformAdapter", event: "MessageEvent", e: BaseException
) -> Optional[dict]:
    """Tell the user a turn failed rather than leaving radio silence (last resort:
    a failing notice is logged, never raised). Returns the thread metadata used."""
    from gateway.platforms.base import _thread_metadata_for_event
    from gateway.warning_notifications import diagnostic_wake_muted

    _thread_metadata = None
    try:
        _thread_metadata = _thread_metadata_for_event(event)
        from agent.operator_details import operator_details_hidden

        if operator_details_hidden():
            notice = t("gateway.notify.turn_error_generic")
        else:
            error_detail = str(e)[:300] if str(e) else t("gateway.notify.turn_error_no_details")
            notice = t(
                "gateway.notify.turn_error_detailed",
                error_type=type(e).__name__,
                error_detail=error_detail,
            )
        # Only the policy reads bind the routed profile; the send stays in the launch scope
        # as before, so delivery bookkeeping keeps landing where boot-time recovery reads it.
        with adapter._media_delivery_scope(event.source):
            content = (
                None
                if diagnostic_wake_muted(event)
                else adapter.warning_text(
                    notice,
                    t("gateway.notify.turn_error"),
                    logical_platform=event.source.platform,
                    chat_id=event.source.chat_id,
                    metadata=_thread_metadata,
                )
            )
        if content is None:
            return _thread_metadata
        await adapter.send(
            chat_id=event.source.chat_id, content=content, metadata=_thread_metadata
        )
    except Exception as notify_err:
        logger.error(
            "[%s] Failed to send error notification to user: %s",
            adapter.name,
            notify_err,
            exc_info=True,
        )
    return _thread_metadata
