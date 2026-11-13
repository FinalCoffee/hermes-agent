#!/bin/sh
# Watch the bot's conversations as they happen: every user message, tool call, tool result and reply,
# read from the session database inside the running container (read-only).
#
#   ./watch.sh          # the last 20 messages, then follow
#   ./watch.sh 200      # the last 200 messages, then follow
#   ./watch.sh 0        # only what arrives from now on
#
# Ctrl-C stops watching; the bot is not affected. These are your users' messages: treat them as such.
set -eu
cd "$(dirname "$0")"

exec docker compose exec -T -u hermes -e WATCH_BACKLOG="${1:-20}" -e WATCH_UTC_OFFSET="$(date +%z)" \
    bot /opt/hermes/.venv/bin/python -u -c '
import json, os, sqlite3, time
from datetime import datetime, timedelta, timezone

raw = os.environ["WATCH_UTC_OFFSET"]
zone = timezone((1 if raw[0] == "+" else -1) * timedelta(hours=int(raw[1:3]), minutes=int(raw[3:5])))
db = sqlite3.connect("file:/opt/data/state.db?mode=ro", uri=True)
QUERY = """SELECT m.id, m.timestamp, m.role, m.content, m.tool_calls, m.tool_name,
                  s.source, s.user_id, s.chat_type, m.session_id
           FROM messages m JOIN sessions s ON s.id = m.session_id WHERE m.id > ? ORDER BY m.id"""


def short(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + " ..."


def show(row):
    _, stamp, role, content, tool_calls, tool_name, source, user, chat_type, session = row
    place = "" if chat_type in (None, "", "dm") else " " + chat_type
    who = (source or "?") + " " + (user or "-") + place
    head = f"{datetime.fromtimestamp(stamp, zone):%m-%d %H:%M:%S}  [{who} #{session[-6:]}]"
    if role == "user":
        print(f"{head}\n    USER   {short(content, 2000)}")
    elif role == "tool":
        print(f"{head}\n    RESULT {tool_name or chr(63)}: {short(content, 600)}")
    else:
        for call in json.loads(tool_calls) if tool_calls else ():
            fn = call.get("function") or call
            name, arguments = fn.get("name"), short(str(fn.get("arguments", "")), 400)
            print(f"{head}\n    TOOL   {name}({arguments})")
        if (content or "").strip():
            print(f"{head}\n    BOT    {short(content, 4000)}")


backlog = int(os.environ["WATCH_BACKLOG"])
last = db.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0]
seen = max(0, last - backlog)
print(f"watching conversations (showing the last {min(backlog, last)} messages first); Ctrl-C to stop", flush=True)
try:
    while True:
        for row in db.execute(QUERY, (seen,)):
            show(row)
            seen = row[0]
        time.sleep(1)
except KeyboardInterrupt:
    pass
'
