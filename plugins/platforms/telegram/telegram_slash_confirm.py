"""Slash-command confirmation taps (``sc:<choice>:<confirm_id>``), kept out of the adapter facade.

The runner parks a confirmation keyed by session; a tap resolves it, edits the prompt with the decision
and sends any text the confirmed command returns into the prompt's topic.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram.constants import ChatType, ParseMode

from agent.i18n import t
from plugins.platforms.telegram.callback_answer import (
    TOAST_LIMIT as _TOAST_LIMIT,
    answer_callback,
)
from plugins.platforms.telegram.telegram_resolved_prompt import edit_keeping_prompt

logger = logging.getLogger("plugins.platforms.telegram.adapter")


async def handle(adapter: Any, query: Any, data: str, cb: dict[str, Any]) -> None:
    """``sc:<choice>:<confirm_id>`` — resolve a slash-command confirmation."""
    from plugins.platforms.telegram.adapter import (
        _toast,
        _unauthorized,
    )  # late: the adapter imports this module

    parts = data.split(":", 2)
    if len(parts) != 3:
        return
    choice = parts[1]  # once, always, cancel
    confirm_id = parts[2]
    resolved = _toast("platform.telegram.slash_confirm.already_resolved")
    if not (
        session_key := await adapter._claim_callback_state(
            query,
            cb,
            adapter._slash_confirm_state,
            confirm_id,
            _unauthorized(),
            resolved,
            pop=False,
        )
    ):
        return
    from gateway.approval_actor import refusal_text, slash_confirm_refusal

    if requester := slash_confirm_refusal(
        session_key,
        confirm_id,
        getattr(query.from_user, "id", ""),
        extra=adapter.config.extra,
        choice=choice,
    ):
        return await answer_callback(
            query, refusal_text(requester)[:_TOAST_LIMIT], show_alert=True
        )
    if not adapter._slash_confirm_state.pop(
        confirm_id, None
    ):  # a concurrent tap claimed it first
        return await answer_callback(query, resolved)
    label_key = {
        "once": "slash_confirm.resolved_once",
        "always": "slash_confirm.resolved_always",
        "cancel": "slash_confirm.resolved_cancel",
    }.get(choice, "approval.resolved_generic")
    user_display = getattr(query.from_user, "first_name", None) or t(
        "platform.telegram.user_fallback"
    )
    label = t(f"platform.telegram.{label_key}")
    await answer_callback(query, label[:_TOAST_LIMIT])
    decided = t(
        "platform.telegram.approval.resolved_by_user", label=label, user=user_display
    )
    await edit_keeping_prompt(adapter, query, decided)
    # The runner stored a handler keyed by session_key; run it and send any returned text as a follow-up.
    try:
        from tools import slash_confirm as _slash_confirm_mod

        result_text = await _slash_confirm_mod.resolve(session_key, confirm_id, choice)
        if result_text and query.message:
            # Inherit the prompt's topic: forums use message_thread_id; private DM-topic lanes need
            # both the topic id and the prompt reply anchor.
            thread_id = getattr(query.message, "message_thread_id", None)
            chat_type = getattr(getattr(query.message, "chat", None), "type", None)
            prompt_message_id = getattr(query.message, "message_id", None)
            send_kwargs: dict[str, Any] = {
                "chat_id": int(query.message.chat_id),
                "text": adapter.format_message(result_text),
                "parse_mode": ParseMode.MARKDOWN_V2,
                **adapter._link_preview_kwargs(),
            }
            is_private_chat = str(getattr(chat_type, "value", chat_type)).lower() in {
                "private",
                str(ChatType.PRIVATE).lower(),
                str(getattr(ChatType.PRIVATE, "value", ChatType.PRIVATE)).lower(),
            }
            if thread_id is not None:
                meta: dict[str, Any] = {"thread_id": str(thread_id)}
                reply_to_id = None
                if is_private_chat and prompt_message_id is not None:
                    reply_to_id = send_kwargs["reply_to_message_id"] = int(
                        prompt_message_id
                    )
                    meta["telegram_dm_topic_reply_fallback"] = True
                send_kwargs.update(
                    adapter._thread_kwargs_for_send(
                        str(query.message.chat_id),
                        str(thread_id),
                        meta,
                        reply_to_message_id=reply_to_id,
                        reply_to_mode=adapter._reply_to_mode,
                    )
                )
            await adapter._send_message_with_thread_fallback(**send_kwargs)
    except Exception as exc:
        logger.error(
            "[%s] slash-confirm callback failed: %s", adapter.name, exc, exc_info=True
        )
