#!/usr/bin/env python3
"""One-shot helper: turn a Google OAuth client into a refresh token.

Run this on a machine with a browser (your laptop, not a container). It opens
Google's consent screen, captures the redirect on localhost, exchanges the code,
and prints the three environment variables gsheets.py needs.

    python3 get_token.py --client-id XXX --client-secret YYY

Only depends on the standard library, so there is nothing to install.

Why this exists: gsheets.py runs somewhere headless, but obtaining a Google
credential requires an interactive login as you. This script confines that
interactive step to one command on a machine you control; the resulting refresh
token is then portable to wherever gsheets.py runs.
"""

from __future__ import annotations

import argparse
import http.server
import json
import secrets
import socket
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"

SUCCESS_PAGE = b"""<!doctype html>
<title>Authorized</title>
<body style="font-family: system-ui, sans-serif; padding: 3rem; max-width: 34rem">
<h2>Authorization complete</h2>
<p>You can close this tab and return to your terminal.</p>
</body>"""

FAILURE_PAGE = b"""<!doctype html>
<title>Authorization failed</title>
<body style="font-family: system-ui, sans-serif; padding: 3rem; max-width: 34rem">
<h2>Authorization failed</h2>
<p>Check the terminal for details.</p>
</body>"""


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the ?code=... Google sends back to our localhost redirect."""

    result: dict[str, str] = {}

    def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}

        ok = "code" in _CallbackHandler.result
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(SUCCESS_PAGE if ok else FAILURE_PAGE)

    def log_message(self, *_args):
        pass  # keep the terminal clean


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _exchange(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode()

    request = urllib.request.Request(TOKEN_URI, data=body)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"Token exchange failed (HTTP {exc.code}):\n{detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Obtain a Google OAuth refresh token for gsheets.py")
    parser.add_argument("--client-id", required=True, help="OAuth client ID (Desktop app type)")
    parser.add_argument("--client-secret", required=True, help="OAuth client secret")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="request read-only access instead of read/write",
    )
    args = parser.parse_args()

    scope = SCOPE + ".readonly" if args.read_only else SCOPE
    port = _free_port()
    redirect_uri = f"http://localhost:{port}/"
    state = secrets.token_urlsafe(16)

    auth_url = f"{AUTH_URI}?" + urllib.parse.urlencode(
        {
            "client_id": args.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            # Required together to actually receive a refresh token:
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )

    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print(f"\nAdd this exact redirect URI to your OAuth client if it is not there yet:\n  {redirect_uri}\n")
    print("Opening your browser to authorize...")
    print(f"If it does not open, visit:\n\n{auth_url}\n")
    webbrowser.open(auth_url)

    server.socket.settimeout(300)
    for _ in range(300):
        if _CallbackHandler.result:
            break
        threading.Event().wait(1)

    result = _CallbackHandler.result
    if not result:
        raise SystemExit("Timed out waiting for authorization.")
    if "error" in result:
        raise SystemExit(f"Authorization denied: {result['error']}")
    if result.get("state") != state:
        raise SystemExit("State mismatch - aborting to avoid a CSRF-substituted code.")

    tokens = _exchange(result["code"], args.client_id, args.client_secret, redirect_uri)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise SystemExit(
            "Google did not return a refresh token. This usually means the account "
            "previously authorized this client. Revoke it at "
            "https://myaccount.google.com/permissions and run this again."
        )

    print("\nSuccess. Set these where gsheets.py runs:\n")
    print(f"export GOOGLE_OAUTH_CLIENT_ID='{args.client_id}'")
    print(f"export GOOGLE_OAUTH_CLIENT_SECRET='{args.client_secret}'")
    print(f"export GOOGLE_OAUTH_REFRESH_TOKEN='{refresh_token}'")
    print("\nTreat the refresh token like a password - it grants access to your sheets.\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
