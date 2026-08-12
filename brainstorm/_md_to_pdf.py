"""Render a brainstorm markdown note to PDF.

Most notes in this folder ship as `<name>.md` + `<name>.pdf`. The PDF is the
artefact people read; the markdown is what makes the next revision a diff
rather than a rewrite. `data-gap-identification.pdf` was generated directly
once, with no source — and could not be revised without reconstructing its
text, because no PDF extractor is installed here. Hence this.

Handles the subset the notes actually use: headings, bullets, numbered lists,
fenced code, pipe tables, `---` rules, `**bold**`, `_italic_`, `` `code` ``.

    python -m brainstorm._md_to_pdf brainstorm/data-gap-identification.md

Code blocks use `Preformatted`, never `Paragraph`: Paragraph collapses
newlines, which silently turns a code block into one run-on line.
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (HRFlowable, ListFlowable, ListItem,
                                PageBreak, Paragraph, Preformatted,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

_BASE = getSampleStyleSheet()
_INK = colors.HexColor("#1a1a1a")
_MUTED = colors.HexColor("#5b6470")
_RULE = colors.HexColor("#d6dbe1")
_CODE_BG = colors.HexColor("#f5f6f8")

S = {
    "h1": ParagraphStyle("h1", parent=_BASE["Heading1"], fontSize=20, leading=25,
                         spaceBefore=2, spaceAfter=8, textColor=_INK),
    "h2": ParagraphStyle("h2", parent=_BASE["Heading2"], fontSize=14, leading=19,
                         spaceBefore=16, spaceAfter=6, textColor=_INK),
    "h3": ParagraphStyle("h3", parent=_BASE["Heading3"], fontSize=11.5, leading=16,
                         spaceBefore=11, spaceAfter=4, textColor=_INK),
    "body": ParagraphStyle("body", parent=_BASE["BodyText"], fontSize=9.6,
                           leading=14.5, spaceAfter=7, textColor=_INK,
                           alignment=TA_LEFT),
    "meta": ParagraphStyle("meta", parent=_BASE["BodyText"], fontSize=9,
                           leading=13, textColor=_MUTED, spaceAfter=10),
    "quote": ParagraphStyle("quote", parent=_BASE["BodyText"], fontSize=10,
                            leading=15, leftIndent=10, spaceAfter=8,
                            textColor=colors.HexColor("#33383f"),
                            borderPadding=(0, 0, 0, 6)),
    "code": ParagraphStyle("code", parent=_BASE["Code"], fontSize=8.2,
                           leading=11.4, textColor=colors.HexColor("#24292f"),
                           backColor=_CODE_BG, borderPadding=6,
                           spaceBefore=3, spaceAfter=9),
    "cell": ParagraphStyle("cell", parent=_BASE["BodyText"], fontSize=8.4,
                           leading=11.5, spaceAfter=0, textColor=_INK),
    "cellh": ParagraphStyle("cellh", parent=_BASE["BodyText"], fontSize=8.4,
                            leading=11.5, spaceAfter=0, textColor=_INK,
                            fontName="Helvetica-Bold"),
}


def _inline(text: str) -> str:
    """Markdown inline spans -> reportlab markup, escaping everything else."""
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`",
                 r'<font face="Courier" size="8.6" color="#8a3ffc">\1</font>',
                 out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", out)
    out = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<i>\1</i>", out)
    return out


def _table(rows: list[list[str]]):
    head, body = rows[0], rows[1:]
    data = [[Paragraph(_inline(c), S["cellh"]) for c in head]]
    data += [[Paragraph(_inline(c), S["cell"]) for c in r] for r in body]
    n = len(head)
    # First column carries the situation name and needs the room; the rest split
    # what is left evenly.
    avail = A4[0] - 40 * mm
    widths = [avail * (0.34 if n > 2 else 0.5)] + \
             [avail * (0.66 / (n - 1) if n > 1 else 0)] * (n - 1)
    t = Table(data, colWidths=widths, hAlign="LEFT", repeatRows=1)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, _RULE),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, colors.HexColor("#eceff2")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
    ]))
    return t


def build(md: str) -> list:
    flow: list = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]

        if ln.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            flow.append(Preformatted("\n".join(buf), S["code"]))
            continue

        if ln.startswith("|") and i + 1 < len(lines) and set(
                lines[i + 1].replace("|", "").strip()) <= set("-: "):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                # Keep every row that is not the `|---|:--|` separator. This
                # was a strict-SUPERSET test, which additionally demanded the
                # row contain a colon — so all seven body rows were dropped and
                # the table rendered as a lone header.
                if not set("".join(cells)) <= set("-: "):
                    rows.append(cells)
                i += 1
            flow += [_table(rows), Spacer(1, 9)]
            continue

        if re.match(r"^(-{3,}|\*{3,})$", ln.strip()):
            flow += [Spacer(1, 4),
                     HRFlowable(width="100%", thickness=0.6, color=_RULE),
                     Spacer(1, 8)]
            i += 1
            continue

        if ln.startswith("### "):
            flow.append(Paragraph(_inline(ln[4:]), S["h3"])); i += 1; continue
        if ln.startswith("## "):
            flow.append(Paragraph(_inline(ln[3:]), S["h2"])); i += 1; continue
        if ln.startswith("# "):
            flow.append(Paragraph(_inline(ln[2:]), S["h1"])); i += 1; continue

        if ln.startswith("> "):
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i].lstrip("> ").rstrip())
                i += 1
            flow.append(Paragraph(_inline(" ".join(buf)), S["quote"]))
            continue

        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", ln)
        if m:
            items, ordered = [], bool(re.match(r"\d+\.", m.group(2)))
            while i < len(lines):
                mm_ = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[i])
                if not mm_:
                    # A wrapped continuation line belongs to the item above.
                    if lines[i].startswith("  ") and lines[i].strip() and items:
                        items[-1] += " " + lines[i].strip()
                        i += 1
                        continue
                    break
                items.append(mm_.group(3))
                i += 1
            flow.append(ListFlowable(
                [ListItem(Paragraph(_inline(t), S["body"]), leftIndent=13)
                 for t in items],
                bulletType="1" if ordered else "bullet",
                bulletFontSize=7.5, leftIndent=13, spaceAfter=6))
            continue

        if not ln.strip():
            i += 1
            continue

        buf = []
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^(#{1,3} |[-*] |\d+\. |```|\||> |-{3,}$)", lines[i]):
            buf.append(lines[i].strip())
            i += 1
        style = S["meta"] if buf and buf[0].startswith("_Revised") else S["body"]
        flow.append(Paragraph(_inline(" ".join(buf)), style))
    return flow


def render(md_path: Path, pdf_path: Path | None = None) -> Path:
    pdf_path = pdf_path or md_path.with_suffix(".pdf")
    doc = SimpleDocTemplate(
        str(pdf_path), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=17 * mm, bottomMargin=17 * mm,
        title=md_path.stem.replace("-", " "), author="AgenticSys",
    )
    doc.build(build(md_path.read_text(encoding="utf-8")))
    return pdf_path


if __name__ == "__main__":
    for arg in sys.argv[1:] or ["brainstorm/data-gap-identification.md"]:
        out = render(Path(arg))
        print(f"wrote {out} ({out.stat().st_size:,} bytes)")
