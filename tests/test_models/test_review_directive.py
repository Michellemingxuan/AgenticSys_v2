import pytest
from pydantic import ValidationError
from models.types import ReviewDirective, ReviewReport


def test_coherent_directive_minimal():
    d = ReviewDirective(kind="coherent")
    assert d.kind == "coherent"
    assert d.specialist is None and d.release_specialist is None


def test_needs_redispatch_directive():
    d = ReviewDirective(kind="needs_redispatch", specialist="modeling",
                        anchor="2025-05", why="drivers not anchored to the spike")
    assert d.specialist == "modeling"
    assert d.anchor == "2025-05"


def test_qualified_release_directive():
    d = ReviewDirective(kind="qualified_release", release_specialist="spend_payments")
    assert d.release_specialist == "spend_payments"


def test_review_report_carries_directive_optional():
    r = ReviewReport()  # existing default-constructible report
    assert r.directive is None
    r2 = ReviewReport(directive=ReviewDirective(kind="coherent"))
    assert r2.directive.kind == "coherent"


def test_invalid_kind_rejected():
    with pytest.raises(ValidationError):
        ReviewDirective(kind="ship_it")
