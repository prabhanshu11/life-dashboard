#!/usr/bin/env bash
# Recreate the finance poller's Gmail token file from pass (e.g. after a disk
# wipe or on a new host). gmail_authorize.py stored it there.
set -euo pipefail
ENTRY=google/gmail-finance-token
DEST="${FINANCE_GMAIL_TOKEN_FILE:-$HOME/.local/state/life-dashboard/gmail-token.json}"
mkdir -p "$(dirname "$DEST")"
umask 077
pass show "$ENTRY" > "$DEST.tmp"
mv "$DEST.tmp" "$DEST"
chmod 600 "$DEST"
echo "restored $DEST from pass:$ENTRY"
