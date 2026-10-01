# /// script
# requires-python = ">=3.12"
# dependencies = ["google-auth-oauthlib>=1.2", "google-auth>=2.30"]
# ///
"""One-time Gmail consent for the finance poller (run ON the desktop).

    uv run scripts/gmail_authorize.py ~/Downloads/client_secret_XXXX.json

1. Prints a Google consent URL. Open it in any browser, sign in, allow
   "Read your email" (gmail.readonly).
2. Google redirects to http://localhost:8765/?code=...
   - On the desktop's own browser (or with `ssh -L 8765:127.0.0.1:8765 desktop`
     from the laptop) the script catches it by itself.
   - Otherwise the browser shows "can't connect": copy that whole address from
     the address bar and paste it here (the bare code also works).
3. Writes the token to $FINANCE_GMAIL_TOKEN_FILE
   (default ~/.local/state/life-dashboard/gmail-token.json, mode 600) and
   stores the same JSON in pass at google/gmail-finance-token.
   The client JSON is not needed afterwards; delete it from Downloads.
"""

from __future__ import annotations

import argparse
import http.server
import os
import select
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
PASS_ENTRY = "google/gmail-finance-token"
PORT = 8765


def _code_from(text: str) -> str | None:
    text = text.strip()
    if not text:
        return None
    if "code=" in text:
        q = urllib.parse.urlparse(text).query or text.split("?", 1)[-1]
        return (urllib.parse.parse_qs(q).get("code") or [None])[0]
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("client_json", help="OAuth client JSON (Desktop app) downloaded from Google Cloud Console")
    ap.add_argument("--no-pass", action="store_true", help="skip storing the token in pass")
    args = ap.parse_args()

    token_path = Path(os.environ.get(
        "FINANCE_GMAIL_TOKEN_FILE", "~/.local/state/life-dashboard/gmail-token.json")).expanduser()
    flow = InstalledAppFlow.from_client_secrets_file(args.client_json, SCOPES)
    flow.redirect_uri = f"http://localhost:{PORT}/"
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")

    got: dict[str, str] = {}
    done = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            code = _code_from(self.path)
            if code:
                got["code"] = code
                done.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Gmail connected for the Life Dashboard. You can close this tab.\n"
                             if code else b"No code in this request.\n")

        def log_message(self, *a):
            pass

    try:
        srv = http.server.HTTPServer(("127.0.0.1", PORT), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    except OSError as e:
        srv = None
        print(f"(port {PORT} busy: {e}; paste mode only)")

    print("\nOpen this URL in a browser and allow access:\n")
    print(url)
    print(f"\nWaiting for the redirect to localhost:{PORT} ... or paste the address "
          "(or code) of the 'can't connect' page here and press Enter:")
    while not done.is_set():
        r, _, _ = select.select([sys.stdin], [], [], 0.5)
        if r:
            code = _code_from(sys.stdin.readline())
            if code:
                got["code"] = code
                done.set()
    if srv:
        srv.shutdown()

    flow.fetch_token(code=got["code"])
    creds = flow.credentials
    if not creds.refresh_token:
        print("ERROR: Google returned no refresh token. Remove the app's access at "
              "https://myaccount.google.com/permissions and run this again.", file=sys.stderr)
        return 2
    data = creds.to_json()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(data)
    os.chmod(token_path, 0o600)
    print(f"Token written: {token_path}")

    if not args.no_pass:
        r = subprocess.run(["pass", "insert", "-m", "-f", PASS_ENTRY], input=data, text=True)
        print(f"pass insert {PASS_ENTRY}: {'ok' if r.returncode == 0 else 'FAILED (token file is still fine)'}")
    print("Done. The next timer run (every 30 min) backfills 60 days; to run it now:\n"
          "  systemctl --user start life-finance-sync.service && "
          "journalctl --user -u life-finance-sync -n 20 --no-pager")
    return 0


if __name__ == "__main__":
    sys.exit(main())
