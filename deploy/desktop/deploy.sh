#!/usr/bin/env bash
# Deploy the Life Dashboard as a systemd USER service on the DESKTOP.
# Run this ON the desktop (see deploy/desktop/README.md for the ssh one-liner).
# Idempotent: safe to re-run after every git pull.
#
# One-time prerequisite so the user service survives logout / runs at boot:
#   loginctl enable-linger prabhanshu
# (Not run here on purpose; it needs sudo on some systems.)
set -euo pipefail

REPO_DIR=/home/prabhanshu/Programs/life-dashboard
REPO_URL=git@github.com:prabhanshu11/life-dashboard.git
DEFAULT_BRANCH=master
UNIT_NAME=life-dashboard.service
UNIT_SRC="$REPO_DIR/deploy/desktop/$UNIT_NAME"
UNIT_DIR="$HOME/.config/systemd/user"
PORT=8090
HEALTH_URL="http://127.0.0.1:$PORT/api/health"
PUBLIC_URL="http://100.92.71.80:$PORT/day"

# (a) clone if missing
if [ ! -d "$REPO_DIR/.git" ]; then
    echo "==> Cloning $REPO_URL into $REPO_DIR"
    git clone "$REPO_URL" "$REPO_DIR"
fi

# (b) update to the default branch, fast-forward only
echo "==> Updating $REPO_DIR ($DEFAULT_BRANCH)"
git -C "$REPO_DIR" fetch origin
git -C "$REPO_DIR" checkout "$DEFAULT_BRANCH"
git -C "$REPO_DIR" pull --ff-only origin "$DEFAULT_BRANCH"

# (c) install / (re)start the user unit
echo "==> Installing $UNIT_NAME into $UNIT_DIR"
mkdir -p "$UNIT_DIR"
cp "$UNIT_SRC" "$UNIT_DIR/$UNIT_NAME"
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"
systemctl --user restart "$UNIT_NAME"

# (d) wait for health
echo "==> Waiting for $HEALTH_URL"
for i in $(seq 1 15); do
    if out=$(curl -fsS "$HEALTH_URL" 2>/dev/null); then
        echo "Healthy after ${i}s: $out"
        echo "Dashboard: $PUBLIC_URL"
        exit 0
    fi
    sleep 1
done

echo "ERROR: $HEALTH_URL did not come up within 15s" >&2
journalctl --user -u "${UNIT_NAME%.service}" -n 30 --no-pager >&2 || true
exit 1
