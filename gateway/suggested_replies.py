"""Suggested replies: the model's footer that offers the user its next messages as tap options.

The model ends a reply with ``[suggestions: A | B | C]`` on its own line. Every line that starts with
the marker is a footer and is removed from the reply wherever it sits, on every platform and surface
that takes the final text through this module (the adapter delivery path, the stream consumer and
the reconcile edits). The raw text still reaches streaming TTS, the execution viewer (``message.delta``)
and the ACP final response, so a model that writes a footer is audible or visible there. Whether a
footer becomes buttons is the adapter's call. Pure string functions: no I/O, no adapter state.
"""

from __future__ import annotations

import re

SUGGESTION_LINE = re.compile(
    r"^[ \t]*\[suggestions:[ \t]*([^\]\n]*)\][ \t]*$", re.IGNORECASE
)
PARTIAL_OPEN = "[suggestions:"
# "[" and "[s" are ordinary text; from this many characters a prefix of the opener is treated as the start of a marker.
MIN_PARTIAL = 5
MAX_OPTIONS, MAX_OPTION_CHARS = 4, 40


def _scan(text: str) -> tuple[str, tuple[str, ...]]:
    """One pass shared by the final and the stream view, so they cannot disagree.

    A marker line is one whose text starts with the opener, complete or not, and an unfinished last
    line that is a prefix of the opener. Marker lines are removed; options come from the last complete
    footer. Text that has no marker line is returned as it was.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    last = len(lines) - 1
    kept: list[str] = []
    footer = None
    marked = False
    for index, line in enumerate(lines):
        stripped = line.lstrip().lower()
        unfinished_tail = (
            index == last
            and MIN_PARTIAL <= len(stripped) < len(PARTIAL_OPEN)
            and PARTIAL_OPEN.startswith(stripped)
        )
        if stripped.startswith(PARTIAL_OPEN) or unfinished_tail:
            marked = True
            footer = SUGGESTION_LINE.match(line) or footer
            continue
        kept.append(line)
    if not marked:
        return text, ()
    options: tuple[str, ...] = ()
    if footer is not None:
        parsed = tuple(
            option
            for option in (
                part.strip()[:MAX_OPTION_CHARS] for part in footer.group(1).split("|")
            )
            if option
        )[:MAX_OPTIONS]
        options = parsed if len(parsed) >= 2 else ()
    return "\n".join(kept).rstrip(), options


def split_suggestions(text: str) -> tuple[str, tuple[str, ...]]:
    """Remove every footer line, wherever it sits; return ``(text, options)``.

    The options are the last complete footer's. A footer goes even when it yields fewer than two
    usable options: a footer line is never shown.
    """
    if not text or "[" not in text:
        return text, ()
    return _scan(text)


def strip_suggestions(text: str) -> str:
    return split_suggestions(text)[0]


def hide_partial_suggestions(text: str) -> str:
    """Streaming view: the same removal as ``split_suggestions``, applied to the text received so far.

    Only a marker line still arriving at the end is hidden in advance, so the opener never flashes on
    screen. Text after a complete footer line is kept.
    """
    if "[" not in text:
        return text
    return _scan(text)[0]


def final_message_id(result) -> str | None:
    """The message that carries the keyboard: the last chunk of a split or rich send. Duck-typed on
    purpose: a platform's send result need not be a full ``SendResult``."""
    raw = getattr(result, "raw_response", None)
    ids = raw.get("message_ids") if isinstance(raw, dict) else None
    if ids:
        return str(ids[-1])
    message_id = getattr(result, "message_id", None)
    return str(message_id) if message_id else None
