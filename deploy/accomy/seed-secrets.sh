#!/bin/sh
# Put the bot's secrets into the Compose data volume as /opt/data/.env (owner uid 10000, mode 600).
#
#   ./seed-secrets.sh [--open] /path/to/.env      # from a file
#   ./seed-secrets.sh [--open] - < secrets        # from stdin, e.g. piped from a secrets manager
#
# --open adds GATEWAY_ALLOW_ALL_USERS=true: every Telegram account may talk to the bot. Without it the
# bot answers only the IDs in TELEGRAM_ALLOWED_USERS, and any allow-all line in the input is dropped.
# Read "Running it open to everyone" in plugins/accomy_travel/README.md first.
#
# The values are streamed into the volume and are never printed or passed as arguments. Running this
# again replaces the file; restart the bot afterwards (docker compose restart bot).
set -eu

open=0
if [ "${1:-}" = "--open" ]; then
    open=1
    shift
fi
src="${1:?usage: seed-secrets.sh [--open] <path to .env | ->}"
if [ "$src" != "-" ] && [ ! -r "$src" ]; then
    echo "seed-secrets: cannot read $src" >&2
    exit 1
fi
[ "$src" = "-" ] || exec < "$src"

cd "$(dirname "$0")"

# Every spelling the .env loader accepts: leading spaces, `export`, spaces round the `=`.
allow_all='^[[:space:]]*(export[[:space:]]+)?[A-Z_]*ALLOW_ALL_USERS[[:space:]]*='
# The bot service's own mounts, without its network or the proxy: the one-off container sees the
# data volume and nothing else. The image must exist already (build-image.sh, or docker pull).
{
    grep -Ev "$allow_all" || true
    echo
    if [ "$open" = 1 ]; then echo "GATEWAY_ALLOW_ALL_USERS=true"; fi
} | docker compose run --rm --no-deps --pull never -T --entrypoint /bin/sh bot -c '
    set -eu
    umask 077
    /bin/cat > /opt/data/.env.new
    if ! /bin/grep -q "^TELEGRAM_BOT_TOKEN=." /opt/data/.env.new; then
        /bin/rm -f /opt/data/.env.new
        echo "seed-secrets: no TELEGRAM_BOT_TOKEN in the input" >&2
        exit 1
    fi
    /bin/chown 10000:10000 /opt/data /opt/data/.env.new
    /bin/mv /opt/data/.env.new /opt/data/.env
'
echo "seed-secrets: wrote .env to the data volume ($([ "$open" = 1 ] && echo open to everyone || echo allowlist only))"
