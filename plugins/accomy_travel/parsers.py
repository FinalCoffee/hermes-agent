"""One parse of a stranger's file by ``_document_worker.py``: a process of its own, with a time limit.

``documents`` asks it for the text of a PDF or an office file, and ``ocr`` for pictures of PDF pages. What
comes back is the worker's JSON answer; a parse that fails, hangs or dies becomes an ``ApiError`` whose message
the model may read.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ._common import ApiError

TIMEOUT_SECONDS = 30.0
_WORKER = Path(__file__).with_name("_document_worker.py")

UNREADABLE = {
    "encrypted": "The file is protected by a password. Ask the user for a copy without one.",
    "no_text": (
        "The file holds scanned pages that could not be read. Ask the user to send those pages as photos or "
        "screenshots, which you can see."),
    "malformed": "The file is damaged or is not what its name says. Ask the user to send it again or as a PDF.",
    "too_large": "The file is too large to read. Ask the user for the pages that matter, or a shorter file.",
}


def run(kind: str, path: str, *arguments: object) -> dict:
    """The worker's answer for one job (see ``_document_worker.py`` for the jobs). Raises ``ApiError``."""
    command = [sys.executable, "-I", str(_WORKER), kind, path, *(str(argument) for argument in arguments)]
    try:
        done = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ApiError("unreadable_file", UNREADABLE["too_large"]) from exc
    try:
        result = json.loads(done.stdout)
    except ValueError as exc:  # the parser died before it could answer
        raise ApiError("unreadable_file", UNREADABLE["malformed"]) from exc
    if "error" in result:
        raise ApiError("unreadable_file", UNREADABLE[result["error"]])
    return result
