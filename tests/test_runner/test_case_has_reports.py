"""`_case_has_reports` decides two things at once: whether report_agent is
dispatched, and — via the `[NOTE]` it drives — whether the ANSWER tells the
reviewer that no curated reports exist. A false negative therefore does not
just skip a lookup, it puts a checkable and wrong claim in front of a reviewer.
"""
import runner.turn.conductor as conductor


def _reports_dir(monkeypatch, root):
    monkeypatch.setattr(conductor, "_REPORTS_DIR", root)


def test_top_level_markdown_counts(monkeypatch, tmp_path):
    (tmp_path / "case1").mkdir()
    (tmp_path / "case1" / "executive_summary_exp_0.md").write_text("x")
    _reports_dir(monkeypatch, tmp_path)
    assert conductor._case_has_reports("case1") is True


def test_nested_report_counts(monkeypatch, tmp_path):
    """Regression: the old check used `iterdir()`, so a report filed one
    directory down read as "this case has no reports"."""
    nested = tmp_path / "case2" / "domain"
    nested.mkdir(parents=True)
    (nested / "bureau_exp_0.md").write_text("x")
    _reports_dir(monkeypatch, tmp_path)
    assert conductor._case_has_reports("case2") is True


def test_suffix_match_is_case_insensitive(monkeypatch, tmp_path):
    """Regression: `p.suffix == ".md"` missed `.MD`."""
    (tmp_path / "case3").mkdir()
    (tmp_path / "case3" / "SUMMARY.MD").write_text("x")
    _reports_dir(monkeypatch, tmp_path)
    assert conductor._case_has_reports("case3") is True


def test_txt_report_counts(monkeypatch, tmp_path):
    """`.txt` is in the case-folder lister, so report_agent can read it."""
    (tmp_path / "case4").mkdir()
    (tmp_path / "case4" / "notes.txt").write_text("x")
    _reports_dir(monkeypatch, tmp_path)
    assert conductor._case_has_reports("case4") is True


def test_charts_only_is_not_a_report(monkeypatch, tmp_path):
    """Generated artifacts must not count — otherwise every case that has ever
    rendered a chart claims to have curated reports."""
    charts = tmp_path / "case5" / "charts"
    charts.mkdir(parents=True)
    (charts / "trend.png").write_bytes(b"\x89PNG")
    _reports_dir(monkeypatch, tmp_path)
    assert conductor._case_has_reports("case5") is False


def test_missing_folder_is_no_reports(monkeypatch, tmp_path):
    _reports_dir(monkeypatch, tmp_path)
    assert conductor._case_has_reports("never_seen") is False


def test_io_error_does_not_assert_absence(monkeypatch):
    """An unreadable folder is a fact about the DISK, not about the case.

    Drive-backed report folders throw transiently; returning False there would
    put "this case has no curated reports" in front of a reviewer because a
    network call blipped.
    """
    class _Unreadable:
        def is_dir(self):
            raise OSError("transient network failure")

    class _Root:
        def __truediv__(self, _case_id):
            return _Unreadable()

    monkeypatch.setattr(conductor, "_REPORTS_DIR", _Root())
    assert conductor._case_has_reports("case6") is True


def test_the_two_notes_say_opposite_things():
    """The pair exists so `not_mentioned` cannot be read as "no reports"."""
    assert "NO curated reports" in conductor._NO_REPORTS_NOTE
    assert "HAS curated reports" in conductor._REPORTS_PRESENT_NOTE
    assert "do not address THIS" in conductor._REPORTS_PRESENT_NOTE
