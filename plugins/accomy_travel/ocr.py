"""The text on scanned PDF pages: a page with no text in it is turned into a picture and read by the model.

A scan is a PDF whose pages are photographs of paper. ``_document_worker.py`` draws such a page as a person
would see it, and the profile's own model, asked through the plugin interface (``ctx.llm``), copies out what
is written on it. The model reads Chinese, Japanese and Korean as well as Latin script, the request goes to the
host the bot already talks to, and the plugin holds no key for it. The model is given no tool for this, so a
page that says "ignore your instructions" can at worst produce those words as text, which the assistant then
treats like any other file content.

Reading a page costs a model call with a picture in it, so a file has its first ``PAGES_PER_FILE`` scanned
pages read, and each sender and the bot as a whole have a daily allowance of pages. A page is counted when the
model is asked, whether or not it answers: a call that fails may still be billed. What was read is kept in
this process for as long as the file is, so the next part of the same file costs nothing more, and one file is
read by one caller at a time, so two requests for it never pay twice.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass

from . import _common, attachments, parsers
from ._common import ApiError

logger = logging.getLogger(__name__)

# Scanned pages read per file, and per model call. A scanned travel document is a page or a few.
PAGES_PER_FILE = 8
PAGES_PER_CALL = 4
# Scanned pages a day, per sender and for the whole bot. A page is about 2,200 input tokens.
PAGES_PER_DAY = 40
GLOBAL_PAGES_PER_DAY = 600
# What one page may give: a full page of small print is about 4,000 characters.
PAGE_CHARS = 8_000
_PAGE_TOKENS = 3_000
# The longer side of a page's picture, in pixels: enough for the small print of a ticket.
_PAGE_SIDE = 1600
_MODEL_TIMEOUT = 120.0
_NOTHING = "(no readable text)"
_INSTRUCTIONS = (
    "Transcribe every page image that follows. A marker line stands before each image: write that marker line "
    "exactly as given, then the page's text. Copy all text exactly as written, in reading order, in its original "
    "language: do not translate, summarise, explain or add anything. Keep line breaks, and write table rows with "
    f"' | ' between cells. If a page has no readable text, write {_NOTHING} under its marker.")

ALLOWANCE, FAILED = "allowance", "failed"

_host = None  # the plugin context, kept for its model interface
_budget = _common.RateLimiter()
_lock = threading.Lock()
_read: dict[tuple[str, int], str] = {}  # (cache file name, page) -> its text, empty for a page with none
_READ_MAX = 2000
_busy: dict[str, list] = {}  # cache file name -> [the lock of whoever reads that file, how many hold or await it]


@dataclass(frozen=True)
class Scan:
    texts: dict[int, str]  # the pages that were read; a page with nothing written on it has an empty text
    unread: tuple[int, ...]  # the scanned pages that were not read
    why: str | None = None  # ALLOWANCE or FAILED when a page that would have been read was not


def configure(ctx) -> None:
    global _host
    _host = ctx


def read(path: str, pages: list[int]) -> Scan:
    """Read the scanned *pages* of the PDF at *path*, the first ``PAGES_PER_FILE`` of them. Never raises: a
    page that could not be read is listed as unread, with the reason when it was the allowance or a failure."""
    name = os.path.basename(path)
    wanted = pages[:PAGES_PER_FILE]
    with _one_reader(name):
        with _lock:
            texts = {page: _read[name, page] for page in wanted if (name, page) in _read}
        todo = [page for page in wanted if page not in texts]
        why = _transcribe(path, name, todo, texts) if todo and _host is not None else None
    return Scan(texts=texts, unread=tuple(page for page in pages if page not in texts), why=why)


@contextlib.contextmanager
def _one_reader(name: str):
    """Hold the file *name* for one caller: a second request for it waits, then finds the pages already read."""
    with _lock:
        entry = _busy.setdefault(name, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _lock:
            entry[1] -= 1
            if not entry[1]:
                del _busy[name]


def _transcribe(path: str, name: str, todo: list[int], texts: dict[int, str]) -> str | None:
    """Read the pages in *todo* into *texts*. Returns why a page was left unread, or None when all were read."""
    sender = attachments.owner_of(path) or "unknown"
    allowance = ((f"{sender}:pages", PAGES_PER_DAY, 86400.0), ("global:pages", GLOBAL_PAGES_PER_DAY, 86400.0))
    if _budget.admit(allowance, charge=False) is not None:  # nothing left: do not even draw the pages
        return ALLOWANCE
    try:
        pictures = parsers.run("render", path, _PAGE_SIDE, ",".join(str(page) for page in todo))["images"]
    except ApiError:
        return FAILED
    why = None
    for start in range(0, len(todo), PAGES_PER_CALL):
        asked = todo[start:start + PAGES_PER_CALL]
        batch = [page for page in asked if _budget.admit(allowance) is None]  # as far as the allowance goes
        if not batch:
            return ALLOWANCE
        try:
            found = _ask([(page, base64.b64decode(pictures[str(page)])) for page in batch])
        except Exception:  # health: allow BLE001 -- a model call fails in as many ways as there are providers
            logger.warning("The model could not read scanned pages of %s", name, exc_info=True)
            return FAILED
        with _lock:
            for page, text in found.items():
                texts[page] = _read[name, page] = text[:PAGE_CHARS]
            while len(_read) > _READ_MAX:
                del _read[next(iter(_read))]
        if len(found) < len(batch):  # the model left a page out: it stays unread and may be asked for again
            why = FAILED
        if len(batch) < len(asked):
            return ALLOWANCE
    return why


def _ask(pictures: list[tuple[int, bytes]]) -> dict[int, str]:
    """The model's reading of each page it was shown. A page it did not answer for is absent."""
    # The marker carries a word made up for this call, so no line printed on a page can pass for one.
    word = secrets.token_hex(4)
    blocks: list[dict] = []
    for page, jpeg in pictures:
        blocks.append({"type": "text", "text": f"=== {word} page {page} ==="})
        blocks.append({"type": "image", "data": jpeg, "mime_type": "image/jpeg"})
    answer = _host.llm.complete_structured(
        instructions=_INSTRUCTIONS, input=blocks, max_tokens=_PAGE_TOKENS * len(pictures), timeout=_MODEL_TIMEOUT,
        purpose="ocr").text
    return _pages_of(answer, word, [page for page, _ in pictures])


def _pages_of(answer: str, word: str, asked: list[int]) -> dict[int, str]:
    """The answer cut at its marker lines. Only pages that were asked for count, each once, and a page the
    model says holds nothing readable has an empty text. One page asked and no marker at all: the whole answer
    is that page."""
    marks = list(re.finditer(rf"^.*{word} page (\d+).*$", answer, re.MULTILINE))
    if not marks and len(asked) == 1 and answer.strip():
        return {asked[0]: "" if answer.strip() == _NOTHING else answer.strip()}
    found: dict[int, str] = {}
    for index, mark in enumerate(marks):
        page = int(mark.group(1))
        end = marks[index + 1].start() if index + 1 < len(marks) else len(answer)
        text = answer[mark.end():end].strip()
        if page in asked and page not in found:
            found[page] = "" if text == _NOTHING else text
    return found
