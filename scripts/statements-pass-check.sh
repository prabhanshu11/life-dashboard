#!/usr/bin/env bash
# Statement passwords: health check. Reports, for this machine (and the desktop with --both),
# which of the `pass` entries the statements poller needs exist, and whether each one opens
# its locked PDFs. Prints only OK / FAIL and file names: a password never reaches stdout,
# argv or a file (it flows pass -> pipe -> qpdf --password-file=/dev/stdin).
#
# Usage:
#   scripts/statements-pass-check.sh            # this machine
#   scripts/statements-pass-check.sh --both     # this machine, then `ssh desktop` (or laptop)
#   scripts/statements-pass-check.sh --entries  # presence only, no PDF test (cheap; for polling)
# Exit 0 = every entry present (and every tested PDF opened), 1 otherwise.
#
# PDFs are looked up in STATEMENTS_DIR, else ~/.local/state/life-dashboard/statements
# (desktop, the poller's store), else ~/Programs/statements-unlock (laptop copy for hand trials).
# Entries: finance/statements/<id> (pi/statements_registry.yaml, password.pass_entry).
set -u

IDS=(hdfc_savings hdfc_cc zerodha jio)
BOTH=0 ENTRIES_ONLY=0
for a in "$@"; do
    case "$a" in
        --both) BOTH=1 ;;
        --entries|--entries-only) ENTRIES_ONLY=1 ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "unknown arg: $a" >&2; exit 2 ;;
    esac
done

export PASSWORD_STORE_GPG_OPTS="${PASSWORD_STORE_GPG_OPTS:-} --batch --pinentry-mode error"

dir=""
for d in "${STATEMENTS_DIR:-}" "$HOME/.local/state/life-dashboard/statements" "$HOME/Programs/statements-unlock"; do
    [ -n "$d" ] && [ -d "$d" ] && { dir="$d"; break; }
done

echo "== $(hostname): pass entries for statements =="
rc=0
present=0
for id in "${IDS[@]}"; do
    entry="finance/statements/$id"
    out=$(timeout 20 pass show "$entry" 2>&1 >/dev/null); r=$?
    if [ $r -eq 124 ]; then
        echo "  $entry: TIMEOUT (gpg lock or locked key)"; rc=1; continue
    elif [ $r -ne 0 ]; then
        if grep -q "not in the password store" <<<"$out"; then
            echo "  $entry: missing"
        else
            echo "  $entry: cannot decrypt (pass exit $r)"
        fi
        rc=1; continue
    fi
    present=$((present + 1))
    if [ $ENTRIES_ONLY -eq 1 ] || [ -z "$dir" ]; then
        echo "  $entry: present"; continue
    fi
    echo "  $entry: present"
    found=0
    while IFS= read -r -d '' f; do
        found=1
        base=$(basename "$f")
        if ! qpdf --is-encrypted "$f" 2>/dev/null; then
            echo "      open (not encrypted)  $base"; continue
        fi
        timeout 20 pass show "$entry" 2>/dev/null | head -n 1 \
            | timeout 60 qpdf --password-file=/dev/stdin --check "$f" >/dev/null 2>&1
        q=$?
        case $q in
            0|3) echo "      OK    $base" ;;
            2)   echo "      FAIL  $base (wrong password)"; rc=1 ;;
            *)   echo "      FAIL  $base (qpdf exit $q)"; rc=1 ;;
        esac
    done < <(find "$dir/$id" -maxdepth 1 -type f -iname '*.pdf' ! -name '*.open.pdf' -print0 2>/dev/null | sort -z)
    [ $found -eq 0 ] && echo "      (no PDFs under $dir/$id to test)"
done
echo "  present: $present/${#IDS[@]}${dir:+  pdf dir: $dir}"

if [ $BOTH -eq 1 ]; then
    case "$(hostname)" in omarchy-desktop) remote=laptop ;; *) remote=desktop ;; esac
    args=()
    [ $ENTRIES_ONLY -eq 1 ] && args+=(--entries)
    if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$remote" bash -s -- "${args[@]}" < "$0"; then
        rc=1
    fi
fi
exit $rc
