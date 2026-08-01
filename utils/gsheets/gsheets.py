#!/usr/bin/env python3
"""Read and edit Google Sheets from the command line.

Talks to the Sheets REST API directly (sheets.googleapis.com) using google-auth
for credential handling. Deliberately avoids google-api-python-client: the REST
surface we need is small, and the discovery-based client pulls in a much larger
dependency tree.

Authentication -- the first of these that is fully configured wins:

  1. Service account, inline JSON:  GOOGLE_SERVICE_ACCOUNT_JSON
  2. Service account, file path:    GOOGLE_APPLICATION_CREDENTIALS
  3. OAuth refresh token:           GOOGLE_OAUTH_CLIENT_ID
                                    GOOGLE_OAUTH_CLIENT_SECRET
                                    GOOGLE_OAUTH_REFRESH_TOKEN

See README.md in this directory for how to obtain either.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from typing import Any

import google.auth.transport.requests
import requests
from google.auth.exceptions import RefreshError
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials

API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets"
RW_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
RO_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


class SheetsError(Exception):
    """Anything that should reach the user as a clean message rather than a traceback."""


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def _service_account_info() -> dict[str, Any] | None:
    inline = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if inline:
        try:
            return json.loads(inline)
        except json.JSONDecodeError as exc:
            raise SheetsError(
                f"GOOGLE_SERVICE_ACCOUNT_JSON is set but is not valid JSON: {exc}"
            ) from exc

    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if path:
        if not os.path.exists(path):
            raise SheetsError(f"GOOGLE_APPLICATION_CREDENTIALS points at a missing file: {path}")
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    return None


def _oauth_credentials() -> UserCredentials | None:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
    if not (client_id and client_secret and refresh_token):
        return None

    # No `scopes` here on purpose: a refresh token already carries whatever
    # scope it was granted at authorization time (see get_token.py). Asking
    # the token endpoint to refresh into a *different* scope -- e.g. the
    # read-only variant when the token was only ever granted the read/write
    # one -- is rejected with invalid_scope, since that scope was never
    # actually granted. Omitting it just returns a token for the scope(s)
    # already on the refresh token.
    return UserCredentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
    )


def build_credentials(read_only: bool = False):
    info = _service_account_info()
    if info is not None:
        # Service account tokens are minted fresh on every refresh (there is
        # no pre-existing user grant to match), so it's safe to request
        # narrower scope here as defense in depth.
        scopes = [RO_SCOPE if read_only else RW_SCOPE]
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        # Domain-wide delegation: act as a real user instead of the robot account.
        subject = os.environ.get("GOOGLE_IMPERSONATE_SUBJECT", "").strip()
        if subject:
            creds = creds.with_subject(subject)
        return creds

    oauth = _oauth_credentials()
    if oauth is not None:
        return oauth

    raise SheetsError(
        "No Google credentials found. Set one of:\n"
        "  GOOGLE_SERVICE_ACCOUNT_JSON   (inline service-account JSON)\n"
        "  GOOGLE_APPLICATION_CREDENTIALS (path to service-account JSON)\n"
        "  GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET + GOOGLE_OAUTH_REFRESH_TOKEN\n"
        "See utils/gsheets/README.md for setup steps."
    )


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------


class Sheets:
    def __init__(self, read_only: bool = False):
        self._creds = build_credentials(read_only=read_only)
        self._session = requests.Session()

    def _token(self) -> str:
        if not self._creds.valid:
            try:
                self._creds.refresh(google.auth.transport.requests.Request())
            except RefreshError as exc:
                raise SheetsError(_explain_refresh_error(exc)) from exc
        return self._creds.token

    def _call(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._token()}"
        response = self._session.request(
            method, f"{API_ROOT}{path}", headers=headers, timeout=60, **kwargs
        )

        if response.status_code >= 400:
            raise SheetsError(_explain_http_error(response))

        return response.json() if response.content else {}

    def metadata(self, spreadsheet_id: str) -> dict[str, Any]:
        return self._call(
            "GET",
            f"/{spreadsheet_id}",
            params={"fields": "properties.title,sheets.properties"},
        )

    def read(self, spreadsheet_id: str, a1_range: str) -> list[list[str]]:
        payload = self._call(
            "GET",
            f"/{spreadsheet_id}/values/{_quote(a1_range)}",
            params={"majorDimension": "ROWS"},
        )
        return payload.get("values", [])

    def write(
        self, spreadsheet_id: str, a1_range: str, values: list[list[Any]], raw: bool = False
    ) -> dict[str, Any]:
        return self._call(
            "PUT",
            f"/{spreadsheet_id}/values/{_quote(a1_range)}",
            params={"valueInputOption": "RAW" if raw else "USER_ENTERED"},
            json={"values": values},
        )

    def append(
        self, spreadsheet_id: str, a1_range: str, values: list[list[Any]], raw: bool = False
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/{spreadsheet_id}/values/{_quote(a1_range)}:append",
            params={
                "valueInputOption": "RAW" if raw else "USER_ENTERED",
                "insertDataOption": "INSERT_ROWS",
            },
            json={"values": values},
        )

    def clear(self, spreadsheet_id: str, a1_range: str) -> dict[str, Any]:
        return self._call("POST", f"/{spreadsheet_id}/values/{_quote(a1_range)}:clear")

    def title_for_gid(self, spreadsheet_id: str, gid: int) -> str:
        meta = self.metadata(spreadsheet_id)
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("sheetId") == gid:
                return props["title"]
        known = ", ".join(
            f"{s['properties']['title']} (gid={s['properties']['sheetId']})"
            for s in meta.get("sheets", [])
        )
        raise SheetsError(f"No tab with gid={gid}. Available tabs: {known or '(none)'}")


def _explain_refresh_error(exc: RefreshError) -> str:
    """Turn a credential-exchange failure into an actionable message."""
    detail = str(exc)
    lowered = detail.lower()

    if "account not found" in lowered or "invalid_grant" in lowered:
        hint = (
            "The credentials were signed correctly but Google rejected them. "
            "Common causes: the service account was deleted, the key was revoked, "
            "or (for OAuth) the refresh token has expired or been revoked."
        )
    elif "invalid_client" in lowered:
        hint = "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET do not match a real OAuth client."
    elif "invalid_scope" in lowered:
        hint = "The credentials are not authorized for the Sheets scope."
    else:
        hint = "Check that the credential environment variables point at a valid, current key."

    return f"Could not obtain a Google access token: {detail}\n\n{hint}"


def _explain_http_error(response: requests.Response) -> str:
    """Turn a Google API error into something the user can act on."""
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError):
        message = response.text[:400] or f"HTTP {response.status_code}"

    hint = ""
    if response.status_code == 403:
        if "caller does not have permission" in message.lower():
            hint = (
                "\n\nHint: the authenticated identity cannot see this spreadsheet. "
                "If you are using a service account, share the sheet with its "
                "client_email address (Share -> add that address)."
            )
        elif "has not been used" in message.lower() or "is disabled" in message.lower():
            hint = (
                "\n\nHint: enable the Google Sheets API for this project in the "
                "Google Cloud console, then retry in a minute."
            )
    elif response.status_code == 404:
        hint = "\n\nHint: check the spreadsheet ID -- a 404 here usually means a wrong or deleted ID."

    return f"Sheets API error (HTTP {response.status_code}): {message}{hint}"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _quote(a1_range: str) -> str:
    return requests.utils.quote(a1_range, safe="")


def parse_spreadsheet_id(value: str) -> str:
    """Accept either a bare spreadsheet ID or a full Google Sheets URL."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", value)
    return match.group(1) if match else value


def parse_gid(value: str) -> int | None:
    match = re.search(r"[#&?]gid=([0-9]+)", value)
    return int(match.group(1)) if match else None


def resolve_range(client: Sheets, spreadsheet_id: str, target: str, explicit_range: str | None) -> str:
    """Work out the A1 range to operate on.

    An explicit --range always wins. Otherwise, if the target URL carried a gid,
    resolve that tab's title so we act on the tab the user was actually looking at
    rather than silently defaulting to the first one.
    """
    if explicit_range:
        return explicit_range

    gid = parse_gid(target)
    if gid is None:
        return ""  # whole first sheet

    title = client.title_for_gid(spreadsheet_id, gid)
    return f"'{title}'" if not title.startswith("'") else title


def read_input_values(source: str | None, as_json: bool) -> list[list[Any]]:
    text = sys.stdin.read() if source in (None, "-") else open(source, encoding="utf-8").read()

    if as_json:
        data = json.loads(text)
        if not isinstance(data, list):
            raise SheetsError("--json input must be a JSON array of rows.")
        return [row if isinstance(row, list) else [row] for row in data]

    return [row for row in csv.reader(io.StringIO(text))]


def emit(rows: list[list[str]], fmt: str) -> None:
    if fmt == "json":
        json.dump(rows, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return

    delimiter = "\t" if fmt == "tsv" else ","
    writer = csv.writer(sys.stdout, delimiter=delimiter, lineterminator="\n")
    writer.writerows(rows)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gsheets",
        description="Read and edit Google Sheets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  gsheets.py info <url>\n"
            "  gsheets.py read <url> --format csv\n"
            "  gsheets.py read <url> --range 'Sheet1!A1:D20'\n"
            "  cat rows.csv | gsheets.py write <url> --range 'Sheet1!A1'\n"
            "  cat new.csv | gsheets.py append <url> --range 'Sheet1'\n"
        ),
    )
    parser.add_argument("--quiet", action="store_true", help="suppress non-essential output")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_target(p, need_range=False):
        p.add_argument("target", help="spreadsheet URL or ID")
        p.add_argument(
            "--range",
            dest="a1_range",
            help="A1 range, e.g. \"Sheet1!A1:D20\"" + ("" if need_range else " (default: tab from URL gid)"),
        )

    p_info = sub.add_parser("info", help="show spreadsheet title and its tabs")
    p_info.add_argument("target", help="spreadsheet URL or ID")

    p_read = sub.add_parser("read", help="read a range")
    add_target(p_read)
    p_read.add_argument(
        "--format", choices=["csv", "tsv", "json"], default="csv", help="output format (default: csv)"
    )

    p_write = sub.add_parser("write", help="overwrite a range from stdin or a file")
    add_target(p_write, need_range=True)
    p_write.add_argument("--input", help="input file (default: stdin)")
    p_write.add_argument("--json", action="store_true", help="input is a JSON array of rows")
    p_write.add_argument("--raw", action="store_true", help="do not parse formulas/numbers")

    p_append = sub.add_parser("append", help="append rows from stdin or a file")
    add_target(p_append, need_range=True)
    p_append.add_argument("--input", help="input file (default: stdin)")
    p_append.add_argument("--json", action="store_true", help="input is a JSON array of rows")
    p_append.add_argument("--raw", action="store_true", help="do not parse formulas/numbers")

    p_clear = sub.add_parser("clear", help="clear values in a range")
    add_target(p_clear, need_range=True)

    args = parser.parse_args(argv)
    spreadsheet_id = parse_spreadsheet_id(args.target)

    client = Sheets(read_only=args.command in ("info", "read"))

    if args.command == "info":
        meta = client.metadata(spreadsheet_id)
        print(f"Title: {meta.get('properties', {}).get('title', '(unknown)')}")
        print(f"ID:    {spreadsheet_id}")
        print("Tabs:")
        for sheet in meta.get("sheets", []):
            props = sheet["properties"]
            grid = props.get("gridProperties", {})
            print(
                f"  - {props['title']}  (gid={props['sheetId']}, "
                f"{grid.get('rowCount', '?')} rows x {grid.get('columnCount', '?')} cols)"
            )
        return 0

    a1_range = resolve_range(client, spreadsheet_id, args.target, args.a1_range)

    if args.command == "read":
        emit(client.read(spreadsheet_id, a1_range), args.format)
        return 0

    if args.command in ("write", "append"):
        values = read_input_values(args.input, args.json)
        if not values:
            raise SheetsError("No input rows provided.")
        if args.command == "write":
            result = client.write(spreadsheet_id, a1_range, values, raw=args.raw)
            updated = result.get("updatedCells", 0)
            if not args.quiet:
                print(f"Updated {updated} cells in {result.get('updatedRange', a1_range)}")
        else:
            result = client.append(spreadsheet_id, a1_range, values, raw=args.raw)
            updates = result.get("updates", {})
            if not args.quiet:
                print(
                    f"Appended {updates.get('updatedRows', len(values))} rows to "
                    f"{updates.get('updatedRange', a1_range)}"
                )
        return 0

    if args.command == "clear":
        result = client.clear(spreadsheet_id, a1_range)
        if not args.quiet:
            print(f"Cleared {result.get('clearedRange', a1_range)}")
        return 0

    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SheetsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
