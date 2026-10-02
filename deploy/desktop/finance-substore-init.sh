#!/usr/bin/env bash
# One-time (idempotent) setup of the passphrase-less finance pass sub-store on the DESKTOP, so the
# unattended life-statements-sync timer can open protected statement PDFs without a pinentry.
#
# His decision 2026-10-02: a dedicated GPG key with NO passphrase + a SEPARATE store, isolated from
# the main ~/.password-store (whose key needs a pinentry). Trade-off he accepted: a stolen desktop
# disk exposes finance/statements/* (nothing else lives here). The main key is not touched.
#
# Creates: GPG key "life-dashboard finance (unattended) <finance@life-dashboard.local>" (ed25519 cert +
# cv25519 encr, no expiry, no passphrase) in the normal keyring, and ~/.password-store-finance (0700,
# `pass init` to that key). Never put this store in git. Add entries afterwards with
# scripts/statements-pass-insert.sh <id>, check with scripts/statements-pass-check.sh.
set -euo pipefail

UID_MAIL="finance@life-dashboard.local"
UID_FULL="life-dashboard finance (unattended) <$UID_MAIL>"
STORE="${FINANCE_STORE:-$HOME/.password-store-finance}"

if ! gpg --list-keys --with-colons "$UID_MAIL" >/dev/null 2>&1; then
    echo "==> Generating passphrase-less key: $UID_FULL"
    gpg --batch --quiet --pinentry-mode loopback --passphrase "" \
        --quick-generate-key "$UID_FULL" ed25519 cert never
fi
FPR=$(gpg --list-keys --with-colons "$UID_MAIL" | awk -F: '/^fpr/{print $10; exit}')
if ! gpg --list-keys --with-colons "$FPR" | grep -q '^sub.*:e:'; then
    echo "==> Adding encryption subkey"
    gpg --batch --quiet --pinentry-mode loopback --passphrase "" --quick-add-key "$FPR" cv25519 encr never
fi

mkdir -p "$STORE"
chmod 700 "$STORE"
if [ ! -s "$STORE/.gpg-id" ]; then
    echo "==> pass init ($STORE)"
    PASSWORD_STORE_DIR="$STORE" pass init "$FPR" >/dev/null
fi

# Prove it decrypts with no agent cache and no pinentry (the timer's conditions).
printf '%s\n' selftest | PASSWORD_STORE_DIR="$STORE" pass insert -m -f tmp/selftest >/dev/null
gpgconf --kill gpg-agent >/dev/null 2>&1 || true
got=$(PASSWORD_STORE_DIR="$STORE" PASSWORD_STORE_GPG_OPTS="--batch --pinentry-mode error" pass show tmp/selftest | head -n 1)
PASSWORD_STORE_DIR="$STORE" pass rm -f tmp/selftest >/dev/null
rmdir "$STORE/tmp" 2>/dev/null || true
if [ "$got" = selftest ]; then
    echo "OK: $STORE decrypts unattended (key $FPR)"
else
    echo "FAIL: $STORE did not decrypt unattended" >&2
    exit 1
fi
