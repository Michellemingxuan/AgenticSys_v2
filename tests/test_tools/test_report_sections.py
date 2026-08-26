"""`list_report_sections` — the roster behind the UI's Case Report panel."""
from pathlib import Path

from tools.fs_tools import REPORT_SECTIONS, list_report_sections


def _write(folder: Path, name: str, body: str = "# x") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(body)


def test_canonical_layout_maps_every_section_in_order(tmp_path):
    for stem in ("executive_summary", "default_journey", "bureau", "payment_spend",
                 "modeling", "driver", "crossbu", "interestingness", "wcc_notes"):
        _write(tmp_path, f"{stem}_exp_0.md")
    _write(tmp_path, "strategy_0.md")  # NB: no `_exp` — the real file is named this way

    secs = list_report_sections(tmp_path)

    assert [s["key"] for s in secs] == [k for k, _ in REPORT_SECTIONS]
    assert [s["label"] for s in secs][:3] == ["Exec Summary", "Default Journey", "Bureau"]
    assert all(s["markdown"] for s in secs), "every section should have resolved a file"


def test_absent_section_is_reported_not_dropped(tmp_path):
    """A missing report is a fact the reviewer should see, so the section
    survives with `markdown: None` rather than vanishing from the strip."""
    _write(tmp_path, "executive_summary_exp_0.md")

    secs = list_report_sections(tmp_path)

    assert len(secs) == len(REPORT_SECTIONS)
    bureau = next(s for s in secs if s["key"] == "bureau")
    assert bureau["markdown"] is None and bureau["filename"] is None
    assert bureau["label"] == "Bureau"


def test_exp_suffix_is_not_load_bearing(tmp_path):
    """A re-export that bumps `_exp_0` -> `_exp_1` must keep its section
    instead of falling through to the unknown-file tail."""
    _write(tmp_path, "bureau_exp_7.md")

    bureau = next(s for s in list_report_sections(tmp_path) if s["key"] == "bureau")

    assert bureau["filename"] == "bureau_exp_7.md"
    assert len(list_report_sections(tmp_path)) == len(REPORT_SECTIONS)


def test_unknown_report_file_is_appended_not_hidden(tmp_path):
    _write(tmp_path, "executive_summary_exp_0.md")
    _write(tmp_path, "litigation_notes.md")

    secs = list_report_sections(tmp_path)

    extra = secs[len(REPORT_SECTIONS):]
    assert [s["filename"] for s in extra] == ["litigation_notes.md"]
    assert extra[0]["label"] == "Litigation Notes"


def test_binary_export_is_not_offered_as_a_section(tmp_path):
    """Real case folders carry `.docx` exports the reader cannot decode
    (case 11854808010 ships one). `is_report_file` excludes them."""
    _write(tmp_path, "executive_summary_exp_0.md")
    (tmp_path / "report_without_citations.docx").write_bytes(b"PK\x03\x04binary")

    secs = list_report_sections(tmp_path)

    assert all(s["filename"] != "report_without_citations.docx" for s in secs)
    assert len(secs) == len(REPORT_SECTIONS)


def test_long_numerics_are_comma_formatted_like_the_agent_sees_them(tmp_path):
    """The panel and the assistant must print the same figure. `fs_read_file`
    comma-formats 6+ digit runs before the agent ever sees them, so serving
    raw bytes here would show `201800` against the agent's `201,800`."""
    _write(tmp_path, "strategy_0.md", "| Current Global Limit |\n|---|\n| 201800 |")

    strategy = next(s for s in list_report_sections(tmp_path) if s["key"] == "strategy")

    assert "201,800" in strategy["markdown"]
    assert "201800" not in strategy["markdown"]


def test_missing_folder_is_empty_not_an_error(tmp_path):
    assert list_report_sections(tmp_path / "no-such-case") == []


# ── ASCII banner headings ────────────────────────────────────────────────

BANNER = (
    "====================================================\n"
    "Bureau Credit Scores\n"
    "====================================================\n"
    "\n"
    "- **SBFE Score** - no data\n"
)


def test_ascii_banner_becomes_a_real_heading(tmp_path):
    """Left alone, markdown reads the trailing rule as a setext underline and
    the LEADING rule survives as a stray paragraph glued to the title."""
    _write(tmp_path, "bureau_exp_0.md", BANNER)

    md = next(s for s in list_report_sections(tmp_path) if s["key"] == "bureau")["markdown"]

    assert "## Bureau Credit Scores" in md
    assert "====" not in md


def test_thematic_break_is_not_mistaken_for_a_banner(tmp_path):
    """`---` is a legitimate markdown rule and `executive_summary_exp_0.md`
    uses it to separate sections. Rewriting those would destroy formatting."""
    _write(tmp_path, "executive_summary_exp_0.md", "## Real Heading\n\ntext\n\n---\n\nmore\n")

    md = next(s for s in list_report_sections(tmp_path)
              if s["key"] == "executive_summary")["markdown"]

    assert "\n---\n" in md
    assert md.count("## ") == 1


def test_banner_promotion_leaves_numbers_alone(tmp_path):
    """Presentation may change; figures may not."""
    _write(tmp_path, "strategy_0.md",
           "====\nLimits\n====\n\n| New Global Limit |\n|---|\n| 201800 |\n")

    md = next(s for s in list_report_sections(tmp_path) if s["key"] == "strategy")["markdown"]

    assert "## Limits" in md
    assert "201,800" in md


def test_unpaired_rule_is_left_alone(tmp_path):
    """A lone rule with no closing partner is not a banner; leaving it be is
    safer than guessing which neighbouring line was meant to be a title."""
    _write(tmp_path, "bureau_exp_0.md", "====\nJust one rule, no closer\n\nbody\n")

    md = next(s for s in list_report_sections(tmp_path) if s["key"] == "bureau")["markdown"]

    assert "====" in md
    assert "## " not in md


# ── typographic bullets ──────────────────────────────────────────────────

def test_typographic_bullets_become_markdown_lists(tmp_path):
    """Markdown does not recognise U+2022, so a run of `• ...` lines parses as
    ONE paragraph joined by soft breaks — a wall of prose with stray bullet
    characters in it. 8 of the 10 sections are written this way."""
    _write(tmp_path, "payment_spend_exp_0.md",
           "## 1. Dataset Overview\n\n"
           "• Spend records: 3865 present.\n"
           "• Payment records: 186 successful.\n")

    md = next(s for s in list_report_sections(tmp_path)
              if s["key"] == "payment_spend")["markdown"]

    assert "- Spend records: 3865 present." in md
    assert "- Payment records: 186 successful." in md
    assert "•" not in md


def test_existing_markdown_bullets_are_untouched(tmp_path):
    """`executive_summary_exp_0.md` already uses `-`, nested at indent 2.
    Rewriting those would break the nesting it encodes."""
    body = "## Heading\n\n- top level\n  - nested item\n  - another\n"
    _write(tmp_path, "executive_summary_exp_0.md", body)

    md = next(s for s in list_report_sections(tmp_path)
              if s["key"] == "executive_summary")["markdown"]

    assert md == body


def test_bullet_indentation_is_preserved(tmp_path):
    _write(tmp_path, "bureau_exp_0.md", "• top\n  • nested\n")

    md = next(s for s in list_report_sections(tmp_path) if s["key"] == "bureau")["markdown"]

    assert md.splitlines() == ["- top", "  - nested"]


def test_a_bullet_mid_sentence_is_not_a_list_marker(tmp_path):
    """Only a line-leading bullet is a marker; one inside prose is content."""
    _write(tmp_path, "bureau_exp_0.md", "Scores a • b • c on one line.\n")

    md = next(s for s in list_report_sections(tmp_path) if s["key"] == "bureau")["markdown"]

    assert md.strip() == "Scores a • b • c on one line."
