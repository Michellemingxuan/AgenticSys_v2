"""Specialist-callable charting tool.

Adds a ``make_chart`` function tool to each domain specialist so it can
explicitly render a chart when a finding is more interpretable as a graph
than as prose + numbers. The tool writes a KnowledgePoint-shaped entry
into ``app_ctx._specialist_kb[<specialist_name>]`` with ``image_path``
populated, so the existing ``_collect_turn_charts`` path in server.py
embeds the chart under "Supporting charts" in the agent's answer — same
mechanism the auto-distiller pipeline uses, no new collection / serving
code needed.

Per-specialist binding: each specialist gets its own tool instance via
``build_make_chart_tool(specialist_name)``. The factory closes over the
specialist's name so the tool knows which KB list to append to without
needing the caller to identify themselves at invocation time. (We can't
read the calling agent's name from ``RunContextWrapper`` reliably — the
SDK doesn't surface it — so factory binding is the cleanest path.)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agents import RunContextWrapper, function_tool

from tools.viz_renderer import kp_to_vega_spec, render_chart


_VALID_KINDS = ("trend", "bar", "share", "trend_dual", "trend_grid")


def build_make_chart_tool(specialist_name: str):
    """Return a ``function_tool`` bound to ``specialist_name`` for KB writes.

    Use ``strict_mode=False`` so we can accept ``list[dict]`` for the
    points array — strict mode rejects open-ended object schemas.
    """
    @function_tool(
        strict_mode=False,
        name_override="make_chart",
        description_override=(
            "Render a chart from a series of points and surface it in the "
            "reasoning trace (NOT inline in the chat answer). Use AFTER a "
            "data tool (summarize_trend / summarize_by_group / "
            "aggregate_column) produced the numbers; pass that series via "
            "`points`. Multiple variables on the same x-axis (typically "
            "time) belong on ONE chart — pick the kind by scale: `trend` "
            "for same-scale series on a shared y-axis; `trend_dual` for "
            "exactly 2 series on different but related scales (twin y); "
            "`trend_grid` for 3+ series on different scales (N stacked "
            "panels). Be selective — only chart when the visual conveys "
            "what numbers alone can't."
        ),
    )
    async def make_chart(
        ctx: RunContextWrapper,
        topic: str,
        kind: str,
        claim: str,
        points: list[dict],
        x_field: str,
        y_fields: list[str],
        source_call: str,
    ) -> str:
        # ── Input validation: return a structured error string the LLM can
        # read and self-correct from, rather than raising.
        if kind not in _VALID_KINDS:
            return (
                f"[make_chart error] `kind` must be one of "
                f"{list(_VALID_KINDS)}; got {kind!r}. Use 'trend' for line "
                f"charts over time, 'bar' for vertical bars, 'share' for "
                f"horizontal-bar breakdowns sorted by value."
            )
        if not isinstance(points, list) or len(points) < 2:
            n = len(points) if isinstance(points, list) else "n/a"
            return (
                f"[make_chart error] `points` must be a list of 2+ dicts; "
                f"got {type(points).__name__} of len {n}. Pass the series "
                f"from your prior summarize_trend / summarize_by_group call."
            )
        if not all(isinstance(p, dict) for p in points):
            return (
                "[make_chart error] every entry in `points` must be a dict; "
                "got at least one non-dict entry."
            )
        if not topic.strip() or not claim.strip():
            return (
                "[make_chart error] `topic` (snake_case slug) and `claim` "
                "(one-sentence finding) are both required."
            )
        if not isinstance(y_fields, list) or not y_fields:
            return (
                "[make_chart error] `y_fields` must be a non-empty list of "
                "the dict keys in `points` to plot. Pass `[\"value\"]` for "
                "a single series, or e.g. `[\"spend\", \"payment\"]` for "
                "two lines on the same chart."
            )
        if kind == "share" and len(y_fields) > 1:
            return (
                "[make_chart error] `share` (horizontal bar) is single-"
                "series only. Use `kind='bar'` if you need to plot multiple "
                "metrics across the same x categories."
            )
        if kind == "trend_dual" and len(y_fields) != 2:
            return (
                f"[make_chart error] `trend_dual` (twin y-axis) requires "
                f"exactly 2 entries in `y_fields`; got {len(y_fields)}. "
                f"Use `kind='trend'` for a single shared y-axis with 1 or "
                f"more series on the same scale, or `kind='trend_grid'` "
                f"for 2-6 series on different scales."
            )
        if kind == "trend_grid" and not (2 <= len(y_fields) <= 6):
            return (
                f"[make_chart error] `trend_grid` (stacked faceted panels) "
                f"requires between 2 and 6 entries in `y_fields`; got "
                f"{len(y_fields)}. Use `kind='trend'` for a single series, "
                f"or drop the lowest-signal series if you have 7+."
            )

        app_ctx: Any = ctx.context if ctx else None
        kb = getattr(app_ctx, "_specialist_kb", None)
        case_folder = getattr(app_ctx, "case_folder", None)
        turn_id = getattr(app_ctx, "_turn_id", None)
        logger = getattr(app_ctx, "logger", None)

        if kb is None or case_folder is None:
            # Test paths or legacy callers without a full session — we
            # can't render or persist. Surface a clear error so the LLM
            # doesn't pretend a chart exists.
            return (
                "[make_chart error] no session context available — "
                "cannot persist chart. Continue without the chart and "
                "include the numbers in your `evidence` instead."
            )

        # Build a KnowledgePoint-shaped dict matching the auto-distiller's
        # output schema. `confidence='high'` because the specialist
        # explicitly chose to chart this — it's not an inference.
        kp_dict: dict[str, Any] = {
            "topic": topic.strip(),
            "claim": claim.strip(),
            "numbers": points,
            "viz": {"kind": kind, "x_field": x_field, "y_fields": list(y_fields)},
            "source_call": source_call.strip(),
            "captured_at_turn": turn_id,
            "confidence": "high",
        }

        # Vega-Lite spec for downstream / interactive consumers.
        spec = kp_to_vega_spec(kp_dict)
        if spec is not None:
            kp_dict["vega_spec"] = spec

        # Render PNG. Failures from the renderer log + return None — we
        # surface that to the LLM as a structured error so it can re-try
        # with corrected params (likely an axis-field mismatch).
        charts_dir = Path(case_folder) / "charts"
        img_path = render_chart(
            kp_dict, charts_dir, turn_id=turn_id, logger=logger,
        )
        if img_path is None:
            return (
                f"[make_chart error] renderer could not produce a chart for "
                f"topic={topic!r}. Check that `x_field` ({x_field!r}) and "
                f"every entry of `y_fields` ({y_fields!r}) match keys "
                f"actually present in every points entry, and that y values "
                f"parse as numbers. See the `viz_render_failed` event in "
                f"the case log for the exact reason."
            )

        kp_dict["image_path"] = img_path
        kb.setdefault(specialist_name, []).append(kp_dict)

        if logger is not None:
            logger.log("make_chart_tool_invoked", {
                "specialist": specialist_name,
                "topic": topic,
                "kind": kind,
                "n_points": len(points),
                "n_series": len(y_fields),
                "image_path": img_path,
            })

        n_series_label = (
            f"({len(points)} points × {len(y_fields)} series)" if len(y_fields) > 1
            else f"({len(points)} points)"
        )
        return (
            f"[chart created] topic={topic!r} kind={kind!r} "
            f"{n_series_label} → file: {Path(img_path).name}. "
            f"The chart will appear in the reasoning trace this turn. "
            f"Reference the topic in `findings` so the narrative can refer "
            f"to it; do NOT re-render the same chart."
        )

    return make_chart
