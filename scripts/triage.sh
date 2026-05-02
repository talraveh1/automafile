#!/usr/bin/env bash
# Launch an interactive Claude Code session and run /triage. Works from either
# the host (spins up + execs into the container) or from inside the container
# (runs claude directly). Either way you stay attached and see every action.
#
# Permissions are pre-skipped (--dangerously-skip-permissions). Safe here
# because Claude only sees what is bind-mounted into the container:
# /workspace (this repo) and /docs (the documents tree).
set -euo pipefail

PROMPT="${1:-/triage}"

if [ -f /.dockerenv ]; then
    exec claude --dangerously-skip-permissions "$PROMPT"
fi

# else: we're on the host, so spin up the container and exec into it
cd "$(dirname "$0")/.."
docker compose up -d automafile >/dev/null
exec docker compose exec automafile \
    claude --dangerously-skip-permissions "$PROMPT"
