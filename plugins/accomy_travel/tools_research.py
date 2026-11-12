"""research_web: public-web research through Brave's LLM Context endpoint.

The model uses it for facts that change over time (entry rules, advisories, disruptions, prices,
events). Scope is screened before a Brave call is spent, and a per-caller limit caps the cost.
Refusals and rate limits come back as statuses so the model can relay them to the user.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from . import _common, scope
from ._common import ToolSpec

_KEY_ENV = "BRAVE_SEARCH_API_KEY"
_CONTEXT_URL = "https://api.search.brave.com/res/v1/llm/context"
_QUERY_MAX_CHARS = 300
_FRESHNESS = ("pd", "pw", "pm", "py")
_MAX_RESULTS = 8
_MAX_SNIPPETS = 5
_SNIPPET_CHARS = 500
_TITLE_CHARS = 200
_MINUTE_LIMIT = 10
_DAY_LIMIT = 100
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

_GUIDANCE = (
    "Cite the URLs you used. Prefer official government or operator sources for visas, entry rules and "
    "advisories, and check each source's date because web facts go stale."
)
_REFUSED_HINT = (
    "Outside this assistant's public-web research scope. Say so briefly and offer a travel-related "
    "alternative instead."
)

# Keys are "<caller>:minute" and "<caller>:day" so the two windows never share a counter.
_limiter = _common.RateLimiter()

_RESEARCH_WEB_SCHEMA = {
    "name": "research_web",
    "description": (
        "Search the public web for current or factual information the travel tools cannot answer: visa and "
        "entry rules, government travel advisories, transport strikes and disruptions, news that affects a "
        "trip, current prices, festivals and events, and other facts that change over time. Returns up to 8 "
        "sources with title, url, hostname, date and snippets. Cite the urls you used in your answer. It "
        "refuses code, security, privacy and advanced-math requests. Use freshness to limit results by age."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A focused search, e.g. 'Japan visa exemption for Taiwan passport holders'. "
                "At most 300 characters.",
            },
            "freshness": {
                "type": "string",
                "enum": list(_FRESHNESS),
                "description": "Optional recency filter: pd past day, pw past week, pm past month, py past year.",
            },
        },
        "required": ["query"],
    },
}


def _within_limits(caller: str) -> bool:
    return _limiter.allow(f"{caller}:minute", _MINUTE_LIMIT, 60.0) and _limiter.allow(
        f"{caller}:day", _DAY_LIMIT, 86400.0
    )


def _freshness_arg(args: dict) -> str | None:
    value = args.get("freshness")
    if value is None or value == "":
        return None
    if value not in _FRESHNESS:
        raise ValueError("freshness must be one of pd, pw, pm or py.")
    return value


def _results(body: Any) -> list[dict]:
    """Sanitized sources with a usable http(s) URL and at least one snippet."""
    if not isinstance(body, dict):
        return []
    sources = body.get("sources") if isinstance(body.get("sources"), dict) else {}
    grounding = body.get("grounding") if isinstance(body.get("grounding"), dict) else {}
    out: list[dict] = []
    for item in grounding.get("generic") or []:
        url = str(item.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        snippets = [
            _common.clean_text(s, _SNIPPET_CHARS) for s in (item.get("snippets") or [])
        ]
        snippets = [text for text in snippets if text][:_MAX_SNIPPETS]
        if not snippets:
            continue
        source = sources.get(url) if isinstance(sources.get(url), dict) else {}
        age = next(
            (
                a
                for a in (source.get("age") or [])
                if isinstance(a, str) and _ISO_DATE_RE.fullmatch(a)
            ),
            None,
        )
        out.append(
            _common.compact(
                {
                    "title": _common.clean_text(item.get("title"), _TITLE_CHARS),
                    "url": url,
                    "hostname": parsed.hostname,
                    "date": age,
                    "snippets": snippets,
                }
            )
        )
        if len(out) == _MAX_RESULTS:
            break
    return out


@_common.tool_handler
def research_web(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return _common.ok(
            status="refused", reason="empty_query", hint="Pass a focused search query."
        )
    if len(query) > _QUERY_MAX_CHARS:
        return _common.ok(
            status="refused",
            reason="query_too_long",
            hint=f"Shorten the query to at most {_QUERY_MAX_CHARS} characters.",
        )
    category = scope.reject_reason(query)
    if category:
        return _common.ok(
            status="refused",
            reason="out_of_scope",
            category=category,
            hint=_REFUSED_HINT,
        )
    freshness = _freshness_arg(args)
    if not _within_limits(_common.caller_id()):
        return _common.ok(status="rate_limited")

    params: dict[str, Any] = {
        "q": query,
        "count": 10,
        "maximum_number_of_urls": _MAX_RESULTS,
        "maximum_number_of_tokens": 4096,
        "context_threshold_mode": "balanced",
    }
    if freshness:
        params["freshness"] = freshness
    status, body = _common.request_json(
        "GET",
        _CONTEXT_URL,
        headers={"X-Subscription-Token": _common.secret(_KEY_ENV)},
        params=params,
    )
    if status == 429:
        return _common.ok(status="rate_limited")
    if status != 200:
        raise _common.ApiError(
            "unavailable",
            "Web research is unavailable right now. Try again shortly.",
            status=status,
        )

    results = _results(body)
    if not results:
        return _common.ok(
            status="no_results",
            results=[],
            hint="No sources matched. Try different keywords.",
        )
    return _common.ok(
        status="ok", note=_common.UNTRUSTED_NOTE, hint=_GUIDANCE, results=results
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="research_web",
        schema=_RESEARCH_WEB_SCHEMA,
        handler=research_web,
        requires_env=(_KEY_ENV,),
    ),
)
