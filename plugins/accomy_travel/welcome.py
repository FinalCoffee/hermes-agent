"""The texts the travel bot answers /start, /help, /whoami and /commands with, and how they are sent.

ingress answers these commands itself and skips them, so core builds no reply and makes no model call. The
language is the sender's Telegram client language: English for an "en" client, Traditional Chinese for any
other, including a client that sends none. The bilingual rule of the operator-facing notices does not apply
here because the language is known. Buttons go to private chats only: a button in a group is usable by the
one who asked, and a welcome there is text only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reply:
    text: str
    options: tuple[str, ...] = ()


_REPLIES: dict[str, dict[str, Reply]] = {
    "zh": {
        "start": Reply(
            "您好，我是 Accomy 旅遊助理 ✈️\n"
            "我可以幫您規劃行程、比較飯店與機票、查詢賽事與演出門票、景點與天氣，也可以替您下訂單。\n"
            "您可以打字、傳語音訊息，或傳照片與檔案（例如訂位確認、行程表）給我。\n"
            "告訴我您想去哪裡、什麼時候出發吧！",
            ("規劃東京五天行程", "找台北飛大阪的機票", "這週末有什麼活動", "差旅標準是什麼？"),
        ),
        "help": Reply(
            "我可以幫您規劃行程、比較飯店與機票、查詢賽事與演出門票、景點與天氣，也可以替您下訂單。\n"
            "飯店與機票都依差旅標準篩選，想知道內容可以直接問我「差旅標準是什麼」。\n"
            "除了打字，也可以傳語音訊息、照片或檔案（PDF、Word、Excel 等）給我。\n\n"
            "/new 開始新的對話\n"
            "/stop 停止目前的回覆\n"
            "/help 再看一次這段說明",
        ),
    },
    "en": {
        "start": Reply(
            "Hello, I'm the Accomy travel assistant ✈️\n"
            "I can plan your itinerary, compare hotels and flights, find sports and event tickets, "
            "attractions and weather, and place bookings for you.\n"
            "You can type, send a voice message, or send me a photo or a file such as a booking confirmation.\n"
            "Tell me where you'd like to go and when you'd like to leave!",
            ("Plan a 5-day trip to Tokyo", "Find flights from Taipei to Osaka", "What events are on this weekend",
             "What is the travel standard?"),
        ),
        "help": Reply(
            "I can plan your itinerary, compare hotels and flights, find sports and event tickets, "
            "attractions and weather, and place bookings for you.\n"
            "Hotels and flights follow the travel standard: ask me \"What is the travel standard?\" to see it.\n"
            "Besides typing, you can send a voice message, a photo or a file (PDF, Word, Excel and more).\n\n"
            "/new starts a fresh conversation\n"
            "/stop stops the current answer\n"
            "/help shows this message again",
        ),
    },
}


def language_of(event) -> str:
    """``en`` for an English Telegram client, ``zh`` for anything else (including no language at all)."""
    raw = getattr(event, "raw_message", None)
    code = getattr(getattr(raw, "from_user", None), "language_code", None) or ""
    return "en" if code.lower().startswith("en") else "zh"


async def send(gateway, source, event, kind: str) -> None:
    """Send the ``start`` or ``help`` reply through the bot that answers this chat. A failed send is logged and
    never changes the drop: the command is answered by the plugin either way, so it never reaches the model."""
    if gateway is None:
        return
    reply = _REPLIES[language_of(event)][kind]
    try:
        adapter = gateway._delivery_adapter_for(source)
        if adapter is None:
            return
        result = await adapter.send(source.chat_id, reply.text, metadata=gateway._thread_metadata_for_source(source))
        message_id = getattr(result, "message_id", None)
        if kind == "start" and reply.options and source.chat_type == "dm" and result.success and message_id:
            await adapter.attach_suggested_replies(source, str(message_id), list(reply.options))
    except Exception:  # health: allow BLE001 -- a failed welcome is logged; the command is skipped either way
        logger.warning("Could not deliver the travel %s reply", kind, exc_info=True)
