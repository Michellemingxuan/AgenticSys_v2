"""Tests for tools.acropedia."""

from __future__ import annotations

from tools.acropedia import acropedia_lookup


def test_lookup_known_term_dti():
    out = acropedia_lookup("DTI")
    assert out["full_name"] == "Debt-To-Income Ratio"
    assert "43%" in out["explanation"]


def test_lookup_is_case_insensitive():
    assert acropedia_lookup("fico") == acropedia_lookup("FICO")
    assert acropedia_lookup("WCC") == acropedia_lookup("Wcc")


def test_lookup_unknown_term_returns_not_available_fallback():
    out = acropedia_lookup("WHATEVER")
    assert out["full_name"] == "WHATEVER"
    assert "not available" in out["explanation"].lower()


def test_lookup_empty_term_returns_fallback():
    out = acropedia_lookup("")
    assert "not available" in out["explanation"].lower()


def test_lookup_returns_a_copy_not_the_shared_entry():
    """Mutating the returned dict must not leak into future lookups."""
    out1 = acropedia_lookup("DTI")
    out1["full_name"] = "MUTATED"
    out2 = acropedia_lookup("DTI")
    assert out2["full_name"] == "Debt-To-Income Ratio"
