#!/usr/bin/env bash
# Type a statement password ONCE; it is stored in this machine's `pass` AND the other
# machine's (desktop <-> laptop over ssh). The secret travels stdin -> pass only:
# never argv (printf is a bash builtin), never a file, never a log, never echoed.
#
# Usage:
#   scripts/statements-pass-insert.sh hdfc_savings hdfc_cc zerodha jio   # all four
#   scripts/statements-pass-insert.sh zerodha                            # one
#   scripts/statements-pass-insert.sh --local-only jio                   # this machine only
#   REMOTE=laptop scripts/statements-pass-insert.sh ...                  # pick the other host
# Entry names: finance/statements/<id> (pi/statements_registry.yaml). Hints per source:
#   hdfc_savings  HDFC Customer ID
#   hdfc_cc       first 4 letters of the name in CAPITALS + birth DDMM (HDFC's usual recipe)
#   zerodha       PAN in capitals
#   jio           first 4 letters of the registered name in lower case + last 4 digits of the Jio number
# Afterwards: scripts/statements-pass-check.sh --both
set -u

LOCAL_ONLY=0
ids=()
for a in "$@"; do
    case "$a" in
        --local-only) LOCAL_ONLY=1 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) ids+=("$a") ;;
    esac
done
[ ${#ids[@]} -gt 0 ] || { echo "usage: $0 [--local-only] <id> [<id>...]   (hdfc_savings hdfc_cc zerodha jio)" >&2; exit 2; }
[ -t 0 ] || { echo "stdin must be a terminal (the password is typed, never piped in)" >&2; exit 2; }

remote="${REMOTE:-}"
if [ $LOCAL_ONLY -eq 0 ] && [ -z "$remote" ]; then
    case "$(hostname)" in omarchy-desktop) remote=laptop ;; *) remote=desktop ;; esac
fi

rc=0
for id in "${ids[@]}"; do
    entry="finance/statements/$id"
    pw=""; pw2=""
    read -rs -p "$entry (hidden, Enter when done): " pw; echo
    [ -n "$pw" ] || { echo "  empty, skipped"; rc=1; continue; }
    read -rs -p "$entry again: " pw2; echo
    if [ "$pw" != "$pw2" ]; then echo "  mismatch, skipped"; rc=1; pw=""; pw2=""; continue; fi
    if printf '%s\n' "$pw" | pass insert -m -f "$entry" >/dev/null; then
        echo "  $(hostname): stored"
    else
        echo "  $(hostname): pass insert FAILED"; rc=1
    fi
    if [ $LOCAL_ONLY -eq 0 ]; then
        if printf '%s\n' "$pw" | ssh -o BatchMode=yes -o ConnectTimeout=10 "$remote" "pass insert -m -f '$entry' >/dev/null"; then
            echo "  $remote: stored"
        else
            echo "  $remote: pass insert FAILED (is $remote reachable? run with --local-only there)"; rc=1
        fi
    fi
    pw=""; pw2=""
done
unset pw pw2
echo "next: scripts/statements-pass-check.sh --both"
exit $rc
