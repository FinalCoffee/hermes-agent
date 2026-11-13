"""Parse one PDF or office file, in a process of its own.

``documents`` and ``ocr`` run this file as a script and read one JSON object from its output:

    python -I _document_worker.py pdf    <path> <max chars>            the text of a PDF, page by page
    python -I _document_worker.py office <path> <max chars>            an office file as Markdown
    python -I _document_worker.py render <path> <longest side> <pages> PDF pages as JPEG pictures (1,3,4)

A file from a stranger is parsed here, away from the bot: the parent can stop a parse that takes too long, a
parser that crashes takes nothing else with it, and on Linux the process may use only ``MEMORY_BYTES`` of
memory. It imports nothing from the plugin, so it also runs when the plugin is installed outside the source
tree.
"""

import base64
import io
import json
import sys

MEMORY_BYTES = 2 * 1024**3
JPEG_QUALITY = 80


def _pdf(path: str, max_chars: int) -> dict:
    import pypdf

    try:
        reader = pypdf.PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            return {"error": "encrypted"}
        pages = len(reader.pages)
        texts, empty, read, used = [], [], 0, 0
        # Pages are read until the text asked for is found or the file ends; the parent's time limit bounds it.
        while read < pages and used <= max_chars:
            read += 1
            text = (reader.pages[read - 1].extract_text() or "").strip()
            if text:
                texts.append([read, text])
                used += len(text)
            else:
                empty.append(read)
    except MemoryError:
        return {"error": "too_large"}
    except Exception:  # health: allow BLE001 -- a hostile PDF fails pypdf in many ways, and each one means unreadable
        return {"error": "malformed"}
    return {"texts": texts, "pages": pages, "empty_pages": empty, "complete": read == pages}


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
    return {"text": text[:max_chars], "complete": len(text) <= max_chars}


def _render(path: str, longest_side: int, pages: list[int]) -> dict:
    """Each page as a JPEG whose longer side is *longest_side* pixels: what a person would see on that page."""
    import pypdfium2

    images = {}
    try:
        document = pypdfium2.PdfDocument(path)
        for number in pages:
            page = document[number - 1]
            picture = page.render(scale=longest_side / max(page.get_size())).to_pil().convert("RGB")
            encoded = io.BytesIO()
            picture.save(encoded, "JPEG", quality=JPEG_QUALITY)
            images[str(number)] = base64.b64encode(encoded.getvalue()).decode("ascii")
    except MemoryError:
        return {"error": "too_large"}
    except Exception:  # health: allow BLE001 -- a hostile PDF fails the renderer in many ways, and each one means unreadable
        return {"error": "malformed"}
    return {"images": images}


def main() -> None:
    kind, path, size = sys.argv[1], sys.argv[2], int(sys.argv[3])
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    try:
        if kind == "render":
            result = _render(path, size, [int(number) for number in sys.argv[4].split(",")])
        else:
            result = (_pdf if kind == "pdf" else _office)(path, size)
    except MemoryError:
        result = {"error": "too_large"}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
