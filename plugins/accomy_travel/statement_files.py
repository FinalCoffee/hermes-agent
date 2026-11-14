"""Where the PDF of a statement lies until the reply that sends it has gone out.

The gateway attaches a file to a reply only from its media caches, and the chat shows a file under its own name.
So each statement gets a folder of its own under the document cache, with a random name: the file keeps a name
made for the user, and nobody can work out the path of someone else's statement from a booking code.

A statement holds a name, an email and a phone number, so a folder lives for ``KEEP_SECONDS`` and no longer.
The gateway clears the files that lie in the cache itself and never looks into a folder, so the old ones are
removed here: whenever a statement is saved, and whenever a message comes in (ingress.py), so that no turn
starts with one on disk that is past its hour.
"""

from __future__ import annotations

import secrets
import shutil
import time
from contextlib import suppress
from pathlib import Path

from hermes_constants import get_hermes_dir

KEEP_SECONDS = 3600
_FOLDER = "accomy-statements"


def _root() -> Path:
    return get_hermes_dir("cache/documents", "document_cache") / _FOLDER


def sweep() -> None:
    """Remove every statement older than ``KEEP_SECONDS``. Never raises: the inbound gate that calls this must
    not fail over a file. What cannot be read or removed now, such as a folder that another thread's sweep is
    removing, is met again by the next sweep."""
    cutoff = time.time() - KEEP_SECONDS
    with suppress(OSError):  # no statement was ever made in this home
        for folder in _root().iterdir():
            with suppress(OSError):
                if folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder)


def save(reference: str, data: bytes) -> Path:
    """Write the PDF of the order *reference* where a reply may attach it from, and return its path."""
    sweep()
    folder = _root() / secrets.token_urlsafe(16)
    folder.mkdir(mode=0o700, parents=True)
    path = folder / f"Accomy-statement-{reference}.pdf"
    path.write_bytes(data)
    return path
