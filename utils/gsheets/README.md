# gsheets

Read and edit Google Sheets from the command line.

This talks to the Google Sheets REST API (`sheets.googleapis.com`) rather than
scraping the web UI. That is a deliberate choice: in sandboxed CI/agent
environments the browser-facing hosts (`docs.google.com`, `drive.google.com`)
are frequently blocked by network policy while the API host is reachable. The
API is also the only supported way to *write* to a sheet.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Then provide credentials by one of the two routes below.

### Option A — service account (recommended for automation)

Best for unattended use: no browser, no token expiry.

1. In the [Google Cloud console](https://console.cloud.google.com/), create or
   select a project.
2. Enable the **Google Sheets API** for that project
   (APIs & Services → Library → "Google Sheets API" → Enable).
3. APIs & Services → Credentials → **Create credentials → Service account**.
4. Open the new service account → **Keys** → Add key → Create new key → **JSON**.
   A `.json` file downloads.
5. **Share the spreadsheet with the service account.** Open the sheet in Google
   Sheets, click Share, and add the `client_email` value from the JSON file
   (it looks like `something@project-id.iam.gserviceaccount.com`). Give it
   *Viewer* for read-only use, or *Editor* to allow writes.

   This step is the one that is easy to miss. Without it every call returns
   `403 caller does not have permission`, because the service account is a
   distinct identity from your own Google account.

Then point the tool at the key:

```sh
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
# or, to avoid a file on disk:
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat /path/to/key.json)"
```

If your organisation uses domain-wide delegation and you want the service
account to act as a specific user, also set
`GOOGLE_IMPERSONATE_SUBJECT=user@yourdomain.com`.

### Option B — OAuth refresh token (acts as you)

Use this when you want access to every sheet your own account can already see,
without sharing anything. Obtain a refresh token once on a machine with a
browser (via the OAuth playground or your own client), then:

```sh
export GOOGLE_OAUTH_CLIENT_ID=...
export GOOGLE_OAUTH_CLIENT_SECRET=...
export GOOGLE_OAUTH_REFRESH_TOKEN=...
```

Refresh tokens for apps in "Testing" publishing status expire after 7 days;
publish the OAuth consent screen to `In production` for a long-lived token.

## Usage

Every command accepts either a full spreadsheet URL or a bare spreadsheet ID.
When you pass a URL containing `gid=...`, the tool resolves that tab's name so
it operates on the tab you were actually looking at, instead of defaulting to
the first one.

```sh
# List the tabs
./gsheets.py info 'https://docs.google.com/spreadsheets/d/<ID>/edit?gid=123'

# Read the tab named in the URL, as CSV
./gsheets.py read 'https://docs.google.com/spreadsheets/d/<ID>/edit?gid=123'

# Read an explicit range, as JSON
./gsheets.py read <ID> --range 'Sheet1!A1:D20' --format json

# Overwrite a range from a CSV file
./gsheets.py write <ID> --range 'Sheet1!A1' --input rows.csv

# Append rows from stdin
printf 'alice,30\nbob,41\n' | ./gsheets.py append <ID> --range 'Sheet1'

# Clear a range (formatting is preserved; only values are removed)
./gsheets.py clear <ID> --range 'Sheet1!A2:D'
```

### Notes

- `write` and `append` default to `USER_ENTERED`, so `=SUM(A1:A5)` becomes a
  live formula and `1,234` becomes a number. Pass `--raw` to store input
  verbatim as text instead.
- `append` adds rows after the last non-empty row in the target range; give it
  a bare tab name rather than a cell range.
- `read` omits trailing empty rows and columns — this is the API's behaviour,
  so rows may have differing lengths.

## Tests

The pure helpers (URL parsing, CSV/JSON handling, range resolution) are covered
by tests that need neither network nor credentials:

```sh
.venv/bin/python -m unittest test_gsheets -v
```

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `No Google credentials found` | None of the environment variables above are set. |
| `403 caller does not have permission` | The sheet is not shared with the service account's `client_email`. |
| `403 ... API has not been used` | The Sheets API is not enabled for the project. |
| `404` | Wrong spreadsheet ID, or the sheet was deleted. |
| `invalid_grant` on refresh | Key revoked/deleted, or an expired OAuth refresh token. |
