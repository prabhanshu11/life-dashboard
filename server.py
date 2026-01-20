#!/usr/bin/env python3
"""
Tablet Display Controller Server

A simple HTTP server with session-based authentication.
Run with: python server.py

Default credentials (change these):
  Username: admin
  Password: changeme
"""

import http.server
import socketserver
import secrets
import hashlib
import html
import os
import json
from urllib.parse import parse_qs
from http.cookies import SimpleCookie
from datetime import datetime, timedelta

# Configuration
PORT = 9473
HOST = "127.0.0.1"

# Default credentials - CHANGE THESE or set environment variables
# Password is stored as SHA-256 hash
DEFAULT_USERNAME = os.environ.get("TDC_USERNAME", "admin")
DEFAULT_PASSWORD_HASH = hashlib.sha256(
    os.environ.get("TDC_PASSWORD", "changeme").encode()
).hexdigest()

# Session storage (in-memory, clears on restart)
sessions = {}
SESSION_DURATION = timedelta(hours=24)


def generate_session_token():
    """Generate a cryptographically secure session token."""
    return secrets.token_urlsafe(32)


def hash_password(password):
    """Hash password using SHA-256."""
    return hashlib.sha256(password.encode()).hexdigest()


def verify_session(cookie_header):
    """Check if session cookie is valid."""
    if not cookie_header:
        return False

    cookie = SimpleCookie()
    cookie.load(cookie_header)

    if "session" not in cookie:
        return False

    token = cookie["session"].value
    if token not in sessions:
        return False

    # Check expiration
    if datetime.now() > sessions[token]["expires"]:
        del sessions[token]
        return False

    return True


def create_session():
    """Create a new session and return the token."""
    token = generate_session_token()
    sessions[token] = {
        "created": datetime.now(),
        "expires": datetime.now() + SESSION_DURATION,
    }
    return token


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Tablet Display Controller</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .login-box {
            background: #16213e;
            padding: 40px;
            border-radius: 12px;
            width: 100%;
            max-width: 360px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }
        h1 {
            margin-bottom: 24px;
            font-size: 24px;
            text-align: center;
        }
        .form-group {
            margin-bottom: 16px;
        }
        label {
            display: block;
            margin-bottom: 6px;
            font-size: 14px;
            color: #aaa;
        }
        input {
            width: 100%;
            padding: 12px 14px;
            border: 1px solid #0f3460;
            border-radius: 6px;
            background: #1a1a2e;
            color: #eee;
            font-size: 16px;
        }
        input:focus {
            outline: none;
            border-color: #e94560;
        }
        button {
            width: 100%;
            padding: 14px;
            border: none;
            border-radius: 6px;
            background: #e94560;
            color: white;
            font-size: 16px;
            font-weight: 500;
            cursor: pointer;
            margin-top: 8px;
        }
        button:hover { background: #ff6b6b; }
        .error {
            background: #4a1c1c;
            color: #ff8888;
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 16px;
            font-size: 14px;
            text-align: center;
        }
    </style>
</head>
<body>
    <div class="login-box">
        <h1>Tablet Display</h1>
        {error}
        <form method="POST" action="/login">
            <div class="form-group">
                <label for="username">Username</label>
                <input type="text" id="username" name="username" required autofocus>
            </div>
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required>
            </div>
            <button type="submit">Login</button>
        </form>
    </div>
</body>
</html>"""


class AuthHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler with session-based authentication."""

    def __init__(self, *args, **kwargs):
        # Serve files from the script's directory
        directory = os.path.dirname(os.path.abspath(__file__))
        super().__init__(*args, directory=directory, **kwargs)

    def do_GET(self):
        """Handle GET requests."""
        cookie_header = self.headers.get("Cookie")

        # Always allow access to login page
        if self.path == "/login":
            self.send_login_page()
            return

        # Check authentication for all other paths
        if not verify_session(cookie_header):
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return

        # Serve index.html for root
        if self.path == "/":
            self.path = "/index.html"

        super().do_GET()

    def do_POST(self):
        """Handle POST requests (login form)."""
        if self.path != "/login":
            self.send_error(404)
            return

        # Parse form data
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length).decode("utf-8")
        params = parse_qs(post_data)

        username = params.get("username", [""])[0]
        password = params.get("password", [""])[0]

        # Verify credentials
        if username == DEFAULT_USERNAME and hash_password(password) == DEFAULT_PASSWORD_HASH:
            # Create session
            token = create_session()

            # Redirect to app with session cookie
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={int(SESSION_DURATION.total_seconds())}"
            )
            self.end_headers()
        else:
            # Show error
            self.send_login_page(error="Invalid username or password")

    def send_login_page(self, error=""):
        """Send the login page."""
        error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
        content = LOGIN_PAGE.replace("{error}", error_html).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        """Log requests with timestamp."""
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {args[0]}")


def main():
    """Start the server."""
    with socketserver.TCPServer((HOST, PORT), AuthHandler) as httpd:
        print(f"Tablet Display Controller")
        print(f"Server running at http://{HOST}:{PORT}")
        print(f"Default credentials: {DEFAULT_USERNAME} / changeme")
        print(f"Set TDC_USERNAME and TDC_PASSWORD env vars to customize")
        print()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")


if __name__ == "__main__":
    main()
