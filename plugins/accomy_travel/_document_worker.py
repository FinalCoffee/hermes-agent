"""Turn one PDF or office file into text, in a process of its own.

``documents`` runs this file as a script (``python -I _document_worker.py <kind> <path> <max chars>``) and
reads one JSON object from its output. A file from a stranger is parsed here, away from the bot: the parent
can stop a parse that takes too long, a parser that crashes takes nothing else with it, and on Linux the
process may use only ``MEMORY_BYTES`` of memory. It imports nothing from the plugin, so it also runs when the
plugin is installed outside the source tree.
"""

import json
import sys

MEMORY_BYTES = 2 * 1024**3


def _pdf(path: str, max_chars: int) -> dict:
    import pypdf

    try:
        reader = pypdf.PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            return {"error": "encrypted"}
        pages = len(reader.pages)
        parts, empty, read, used = [], [], 0, 0
        # Pages are read until the text asked for is found or the file ends; the parent's time limit bounds it.
        while read < pages and used <= max_chars:
            read += 1
            text = (reader.pages[read - 1].extract_text() or "").strip()
            if text:
                parts.append(f"[page {read}]\n{text}")
                used += len(parts[-1]) + 2
            else:
                empty.append(read)
    except Exception:  # health: allow BLE001 -- a hostile PDF fails pypdf in many ways, and each one means unreadable
        return {"error": "malformed"}
    text = "\n\n".join(parts)
    return {"text": text, "pages": pages, "empty_pages": empty, "complete": read == pages and len(text) <= max_chars}


def _office(path: str, max_chars: int) -> dict:
    import anydoc

    try:
        text = anydoc.to_markdown(path)
    except anydoc.EncryptedError:
        return {"error": "encrypted"}
    except anydoc.NeedsOcrError:
        return {"error": "no_text"}
    except anydoc.ResourceLimitError:
        return {"error": "too_large"}
    except anydoc.ConvertError:
        return {"error": "malformed"}
    return {"text": text, "complete": len(text) <= max_chars}


def main() -> None:
    kind, path, max_chars = sys.argv[1], sys.argv[2], int(sys.argv[3])
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    try:
        result = (_pdf if kind == "pdf" else _office)(path, max_chars)
    except MemoryError:
        result = {"error": "too_large"}
    if "text" in result:
        result["text"] = result["text"][:max_chars]
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
