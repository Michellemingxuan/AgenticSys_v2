"""Unit tests for gateway.case_scrubber."""

import pytest

from gateway.case_scrubber import scrub


def test_scrub_basic_token():
    assert scrub("see CASE-00001 payments") == "see <case> payments"


def test_scrub_case_insensitive():
    assert scrub("see case-00001") == "see <case>"
    assert scrub("see Case-42") == "see <case>"


def test_scrub_multiple_tokens():
    result = scrub("CASE-00001 and CASE-00002")
    assert result == "<case> and <case>"


def test_scrub_embedded_in_json():
    import json
    payload = json.dumps({"ref": "CASE-00007", "other": "fine"})
    scrubbed = scrub(payload)
    assert "CASE-00007" not in scrubbed
    assert "<case>" in scrubbed


def test_scrub_idempotent():
    once = scrub("CASE-00001")
    twice = scrub(once)
    assert once == twice == "<case>"


def test_scrub_empty_and_no_match():
    assert scrub("") == ""
    assert scrub("no case-ish content here") == "no case-ish content here"


def test_scrub_respects_word_boundaries():
    # A string that merely contains the substring "CASE-" as part of a larger token
    # should be scrubbed if followed by digits (that's the whole point), but a bare
    # "CASE-" with no digits should NOT be touched.
    assert scrub("CASE-notanumber") == "CASE-notanumber"
    assert scrub("CASE-") == "CASE-"
