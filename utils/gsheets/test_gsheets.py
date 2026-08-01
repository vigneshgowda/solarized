#!/usr/bin/env python3
"""Tests for the pure helpers in gsheets.py (no network, no credentials)."""

import io
import sys
import unittest
from contextlib import redirect_stdout

import gsheets

USER_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1TKn4vBk2c3RHFoWnruZckrM8-AVVYie1sgJDcsJ6_4E/edit?gid=1898942419#gid=1898942419"
)


class ParseSpreadsheetId(unittest.TestCase):
    def test_extracts_id_from_full_url(self):
        self.assertEqual(
            gsheets.parse_spreadsheet_id(USER_URL),
            "1TKn4vBk2c3RHFoWnruZckrM8-AVVYie1sgJDcsJ6_4E",
        )

    def test_bare_id_passes_through(self):
        self.assertEqual(gsheets.parse_spreadsheet_id("abc123-_XYZ"), "abc123-_XYZ")

    def test_url_without_gid(self):
        url = "https://docs.google.com/spreadsheets/d/SOME_ID_123/edit"
        self.assertEqual(gsheets.parse_spreadsheet_id(url), "SOME_ID_123")


class ParseGid(unittest.TestCase):
    def test_reads_gid_from_query(self):
        self.assertEqual(gsheets.parse_gid(USER_URL), 1898942419)

    def test_reads_gid_from_fragment_only(self):
        self.assertEqual(gsheets.parse_gid("https://x/y#gid=42"), 42)

    def test_absent_gid_is_none(self):
        self.assertIsNone(gsheets.parse_gid("https://docs.google.com/spreadsheets/d/ID/edit"))

    def test_does_not_match_lookalike_param(self):
        self.assertIsNone(gsheets.parse_gid("https://x/y?notgid=5"))


class ReadInputValues(unittest.TestCase):
    def _with_stdin(self, text, **kwargs):
        original, sys.stdin = sys.stdin, io.StringIO(text)
        try:
            return gsheets.read_input_values(None, kwargs.get("as_json", False))
        finally:
            sys.stdin = original

    def test_csv_from_stdin(self):
        self.assertEqual(self._with_stdin("a,b\n1,2\n"), [["a", "b"], ["1", "2"]])

    def test_csv_preserves_quoted_commas(self):
        self.assertEqual(self._with_stdin('"x,y",z\n'), [["x,y", "z"]])

    def test_json_rows(self):
        self.assertEqual(self._with_stdin('[["a",1],["b",2]]', as_json=True), [["a", 1], ["b", 2]])

    def test_json_scalars_become_single_cell_rows(self):
        self.assertEqual(self._with_stdin('["a","b"]', as_json=True), [["a"], ["b"]])

    def test_json_object_rejected(self):
        with self.assertRaises(gsheets.SheetsError):
            self._with_stdin('{"a": 1}', as_json=True)


class Emit(unittest.TestCase):
    def _capture(self, rows, fmt):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            gsheets.emit(rows, fmt)
        return buffer.getvalue()

    def test_csv_output(self):
        self.assertEqual(self._capture([["a", "b"], ["1", "2"]], "csv"), "a,b\n1,2\n")

    def test_tsv_output(self):
        self.assertEqual(self._capture([["a", "b"]], "tsv"), "a\tb\n")

    def test_json_output_roundtrips(self):
        import json

        self.assertEqual(json.loads(self._capture([["a", "b"]], "json")), [["a", "b"]])

    def test_empty_rows(self):
        self.assertEqual(self._capture([], "csv"), "")


class ResolveRange(unittest.TestCase):
    class FakeClient:
        def __init__(self, title="Q3 Data"):
            self.title = title
            self.calls = 0

        def title_for_gid(self, spreadsheet_id, gid):
            self.calls += 1
            return self.title

    def test_explicit_range_wins_and_skips_lookup(self):
        client = self.FakeClient()
        self.assertEqual(gsheets.resolve_range(client, "ID", USER_URL, "Sheet1!A1:B2"), "Sheet1!A1:B2")
        self.assertEqual(client.calls, 0, "should not hit the API when --range is given")

    def test_gid_resolves_to_quoted_tab_title(self):
        client = self.FakeClient("Q3 Data")
        self.assertEqual(gsheets.resolve_range(client, "ID", USER_URL, None), "'Q3 Data'")

    def test_no_gid_yields_empty_range(self):
        client = self.FakeClient()
        url = "https://docs.google.com/spreadsheets/d/ID/edit"
        self.assertEqual(gsheets.resolve_range(client, "ID", url, None), "")


class QuoteRange(unittest.TestCase):
    def test_exclamation_and_space_are_encoded(self):
        self.assertEqual(gsheets._quote("Sheet 1!A1:B2"), "Sheet%201%21A1%3AB2")

    def test_single_quotes_encoded(self):
        self.assertEqual(gsheets._quote("'My Tab'"), "%27My%20Tab%27")


if __name__ == "__main__":
    unittest.main(verbosity=2)
