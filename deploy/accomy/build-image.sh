#!/bin/sh
# Build the bot image from a snapshot of this checkout: tracked files plus new files git does not
# ignore, at their working-tree content. Caches, virtualenvs and git-ignored files (.env) never reach
# the build context. A new file that git does not ignore IS included, so the build stops when one
# looks like a secret or a profile export: ignore it, delete it or move it out of the checkout.
#
#   ./build-image.sh                                  # tags accomy-hermes:local
#   ACCOMY_IMAGE=<registry>/accomy-hermes:poc-1 ./build-image.sh
#   ACCOMY_PLATFORM=linux/amd64 ./build-image.sh      # cross-build for an x86 host
set -eu

repo="$(cd "$(dirname "$0")/../.." && pwd)"
image="${ACCOMY_IMAGE:-accomy-hermes:local}"
cd "$repo"

suspect="$(git ls-files --others --exclude-standard \
    | grep -Ei '(\.env($|\.)|\.key$|\.p12$|\.pfx$|\.tar\.gz$|\.tgz$|(^|/)id_(rsa|ed25519))' || true)"
if [ -n "$suspect" ]; then
    echo "build-image: untracked files that look like secrets or exports would enter the image:" >&2
    echo "$suspect" >&2
    exit 1
fi

revision="$(git rev-parse HEAD)"
[ -z "$(git status --porcelain)" ] || revision="$revision-dirty"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/context"

# Snapshot through a throwaway index: the real index and the working tree are not touched.
cp "$(git rev-parse --git-dir)/index" "$work/index"
GIT_INDEX_FILE="$work/index" git add -A
tree="$(GIT_INDEX_FILE="$work/index" git write-tree)"
git archive "$tree" | tar -xf - -C "$work/context"

docker build ${ACCOMY_PLATFORM:+--platform "$ACCOMY_PLATFORM"} \
    --label "org.opencontainers.image.revision=$revision" \
    -t "$image" "$work/context"
echo "build-image: $image ($revision)"
