"""Telegram sticker descriptions: a static sticker is described once per ``file_unique_id`` by the
vision model and cached; animated and video stickers get an emoji placeholder without a download.

Only reached for a sticker that was accepted (``telegram.accept_attachments``): a refused sticker is
never fetched and never sent to a vision model (``telegram_attachments``)."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("plugins.platforms.telegram.adapter")


async def describe_sticker(adapter: Any, msg: Any, event: Any) -> None:
    """Describe a sticker via vision, cached by file_unique_id; animated/video stickers get an emoji placeholder."""
    from gateway.sticker_cache import (
        get_cached_description, cache_sticker_description_async, build_sticker_injection,
        build_animated_sticker_injection, STICKER_VISION_PROMPT)
    from plugins.platforms.telegram import adapter as tg  # tests patch the fetch and the image cache on the facade
    sticker = msg.sticker
    emoji = sticker.emoji or ""
    set_name = sticker.set_name or ""
    if sticker.is_animated or sticker.is_video:
        event.text = build_animated_sticker_injection(emoji)
        return
    cached = get_cached_description(sticker.file_unique_id)
    if cached:
        event.text = build_sticker_injection(cached["description"], cached.get("emoji", emoji), cached.get("set_name", set_name))
        logger.info("[Telegram] Sticker cache hit: %s", sticker.file_unique_id)
        return
    fallback = f"a sticker with emoji {emoji}" if emoji else "a sticker"
    try:
        _, image_bytes = await tg.fetch_telegram_file(sticker, "sticker", tg.local_root(adapter))
        cached_path = await tg.cache_image_from_bytes_async(bytes(image_bytes), ext=".webp")
        logger.info("[Telegram] Analyzing sticker at %s", cached_path)
        from tools.vision_tools import vision_analyze_tool
        adapter._accept_update()  # Cancellation cannot undo an already submitted auxiliary LLM request.
        result = json.loads(await vision_analyze_tool(image_url=cached_path, user_prompt=STICKER_VISION_PROMPT))
        if result.get("success"):
            description = result.get("analysis", "a sticker")
            await cache_sticker_description_async(sticker.file_unique_id, description, emoji, set_name)
            event.text = build_sticker_injection(description, emoji, set_name)
        else:
            event.text = build_sticker_injection(fallback, emoji, set_name)
    except Exception as e:
        logger.warning("[Telegram] Sticker analysis error: %s", tg._redact_telegram_error_text(e), exc_info=True)
        event.text = build_sticker_injection(fallback, emoji, set_name)
