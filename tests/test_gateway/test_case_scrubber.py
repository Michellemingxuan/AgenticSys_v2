"""Unit tests for gateway.case_scrubber."""

from gateway.case_scrubber import scrub


def test_scrub_replaces_active_case_id():
    assert scrub("see 77165907010 payments", "77165907010") == "see <case> payments"


def test_scrub_handles_multiple_occurrences():
    result = scrub("77165907010 vs 77165907010 again", "77165907010")
    assert result == "<case> vs <case> again"


def test_scrub_respects_word_boundaries():
    # Longer digit runs that contain the case_id as a substring should NOT be scrubbed.
    assert scrub("X77165907010Y", "77165907010") == "X77165907010Y"
    # The case_id flanked by non-digit separators SHOULD be scrubbed.
    assert scrub("case 77165907010, status ok", "77165907010") == "case <case>, status ok"


def test_scrub_embedded_in_json():
    import json
    payload = json.dumps({"ref": "77165907010", "other": "fine"})
    scrubbed = scrub(payload, "77165907010")
    assert "77165907010" not in scrubbed
    assert "<case>" in scrubbed


def test_scrub_only_touches_the_given_case_id():
    # A different 11-digit run is NOT the current case — the scrubber must leave it alone.
    text = "current 77165907010 vs other 99999999999"
    scrubbed = scrub(text, "77165907010")
    assert scrubbed == "current <case> vs other 99999999999"


def test_scrub_idempotent():
    once = scrub("77165907010", "77165907010")
    twice = scrub(once, "77165907010")
    assert once == twice == "<case>"


def test_scrub_no_active_case():
    # With no current case, the scrubber is a no-op.
    assert scrub("anything 77165907010", None) == "anything 77165907010"
    assert scrub("anything 77165907010", "") == "anything 77165907010"


def test_scrub_empty_text():
    assert scrub("", "77165907010") == ""
