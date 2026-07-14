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

from tools.viz_renderer import render_chart


_VALID_KINDS = ("trend", "bar", "share", "trend_dual", "trend_grid", "table",
                 "histogram", "kde", "pie")

# Plot kinds (everything except `table`) need enough datapoints for the
# shape to actually convey something — 4 is the project's minimum (also
# documented in skills/workflow/data_query.md). Below that, the visual
# is just noise compared to the equivalent inline table.
_PLOT_MIN_POINTS = 4


_DATA_VIZ_SKILL_PATH = (
    Path(__file__).parent.parent / "skills" / "workflow" / "data_viz.md"
)

# Cache the skill body once; the file doesn't change at runtime.
_DATA_VIZ_BODY_CACHE: str | None = None


def _load_data_viz_body() -> str:
    """Read skills/workflow/data_viz.md once and cache. Returned as the
    payload of the ``get_chart_guidance()`` lazy-loaded tool — the
    specialist calls it only when about to plot, so its ~7.9 KB of
    chart-construction rules don't bloat the default system prompt for
    questions that never chart.
    """
    global _DATA_VIZ_BODY_CACHE
    if _DATA_VIZ_BODY_CACHE is None:
        try:
            text = _DATA_VIZ_SKILL_PATH.read_text(encoding="utf-8")
            # Strip the YAML frontmatter — the model only needs the body.
            if text.startswith("---"):
                end = text.find("\n---", 3)
                if end > 0:
                    text = text[end + 4:].lstrip()
            _DATA_VIZ_BODY_CACHE = text
        except Exception:  # noqa: BLE001
            _DATA_VIZ_BODY_CACHE = (
                "(chart guidance not available — fall back to the "
                "minimal description on make_chart itself.)"
            )
    return _DATA_VIZ_BODY_CACHE


@function_tool(
    strict_mode=False,
    name_override="get_chart_guidance",
    description_override=(
        "Returns the chart-construction rulebook (how to pick `kind`, "
        "multi-series alignment, threshold reference lines, topic naming, "
        "common errors). Call this ONCE before invoking `make_chart` if "
        "you need detailed guidance beyond the basics in § SYNTHESIZE."
    ),
)
async def get_chart_guidance(ctx: RunContextWrapper) -> str:  # noqa: ARG001
    return _load_data_viz_body()


def build_make_chart_tool(specialist_name: str):
    """Return a ``function_tool`` bound to ``specialist_name`` for KB writes.

    Use ``strict_mode=False`` so we can accept ``list[dict]`` for the
    points array — strict mode rejects open-ended object schemas.
    """
    @function_tool(
        strict_mode=False,
        name_override="make_chart",
        description_override=(
            "Render a chart in the reasoning trace. Call after summarize_trend "
            "/ summarize_by_group returns ≥ 4 data points — pass the COMPLETE "
            "series from the tool result. "
            "`kind` ∈ {trend, bar, share, trend_dual, trend_grid, table, "
            "histogram, kde, pie}: "
            "trend = same-scale lines; trend_dual = twin y; bar = DEFAULT "
            "for categorical (vertical, sorted by value desc); "
            "histogram = value distribution; kde = density curve; "
            "pie = proportional breakdown; table = 1-3 rows."
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
                f"charts over time, 'bar' (DEFAULT) for categorical "
                f"breakdowns — vertical, auto-sorted by value descending. "
                f"Only use 'share' (horizontal bar) as an escape valve when "
                f"labels truly can't fit vertical."
            )
        if not isinstance(points, list) or len(points) < 1:
            n = len(points) if isinstance(points, list) else "n/a"
            return (
                f"[make_chart error] `points` must be a list of 1+ dicts; "
                f"got {type(points).__name__} of len {n}. Pass the series "
                f"from your prior summarize_trend / summarize_by_group call."
            )
        if not all(isinstance(p, dict) for p in points):
            return (
                "[make_chart error] every entry in `points` must be a dict; "
                "got at least one non-dict entry."
            )
        # Enforce the ≥ 4 minimum for actual plots. 1-3 points belong in
        # a table — the route the LLM should take is `kind='table'`,
        # which surfaces a table card in the Plots panel instead of
        # rendering an image.
        if kind != "table" and len(points) < _PLOT_MIN_POINTS:
            return (
                f"[make_chart error] `kind={kind!r}` plots need at least "
                f"{_PLOT_MIN_POINTS} datapoints; got {len(points)}. With "
                f"1-3 rows the chart shape is noise — pass the same "
                f"`points` with `kind='table'` to surface the rows as a "
                f"compact table card in the Plots panel. (No image is "
                f"rendered for tables; the data structure is sent to the "
                f"frontend directly.)"
            )
        if not topic.strip() or not claim.strip():
            return (
                "[make_chart error] `topic` (snake_case slug) and `claim` "
                "(one-sentence finding) are both required."
            )
        if kind != "table" and (not isinstance(y_fields, list) or not y_fields):
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
        emit_event = getattr(app_ctx, "_emit_event", None)

        if kb is None or case_folder is None:
            # Test paths or legacy callers without a full session — we
            # can't render or persist. Surface a clear error so the LLM
            # doesn't pretend a chart exists.
            return (
                "[make_chart error] no session context available — "
                "cannot persist chart. Continue without the chart and "
                "include the numbers in your `evidence` instead."
            )

        # Fire `chart_pending` BEFORE rendering so the frontend can show a
        # "working on plots" placeholder while matplotlib runs (a typical
        # render is sub-second but the actual `chart` event only lands at
        # end-of-turn after the distiller drains — that gap is what the
        # placeholder bridges). The pending event is keyed by (specialist,
        # topic) so the frontend can match it to the eventual chart event.
        if callable(emit_event):
            emit_event("chart_pending", {
                "specialist": specialist_name,
                "topic": topic.strip(),
                "kind": kind,
            })

        # Build a KnowledgePoint-shaped dict matching the auto-distiller's
        # output schema. `confidence='high'` because the specialist
        # explicitly chose to chart this — it's not an inference.
        kp_dict: dict[str, Any] = {
            "topic": topic.strip(),
            "claim": claim.strip(),
            "numbers": points,
            "viz": {
                "kind": kind,
                "x_field": x_field,
                "y_fields": list(y_fields) if isinstance(y_fields, list) else [],
            },
            "source_call": source_call.strip(),
            "captured_at_turn": turn_id,
            "confidence": "high",
        }

        # Table kind: skip the matplotlib render entirely. The frontend
        # renders the `numbers` array as a real HTML table in the Plots
        # panel. We still persist the KP so the existing chart-emit
        # pipeline (server-side `_collect_turn_charts` + `chart` SSE
        # event) picks it up.
        if kind == "table":
            kb.setdefault(specialist_name, []).append(kp_dict)
            n_cols = len(y_fields) if y_fields else len(points[0]) if points else 0
            if logger is not None:
                logger.log("make_chart_tool_invoked", {
                    "specialist": specialist_name,
                    "topic": topic,
                    "kind": kind,
                    "n_points": len(points),
                    "n_series": len(y_fields) if isinstance(y_fields, list) else 0,
                    "image_path": None,
                })
            return (
                f"[chart created] topic={topic!r} kind='table' "
                f"({len(points)} rows × {n_cols} columns). The "
                f"table will surface in the Plots panel this turn. "
                f"Reference the topic in `findings` so the narrative "
                f"can refer to it; do NOT re-render."
            )

        # Auto-inject thresholds from the catalog before rendering.
        catalog = getattr(app_ctx, "_catalog", None)
        if catalog and hasattr(catalog, "get_thresholds"):
            for table_name in (catalog.list_tables() if hasattr(catalog, "list_tables") else []):
                thresholds = catalog.get_thresholds(table_name)
                for yf in y_fields:
                    th_key = f"threshold_{yf}" if len(y_fields) > 1 else "threshold"
                    if yf in thresholds and not any(
                        p.get(th_key) is not None for p in points
                    ):
                        th_val = thresholds[yf]["value"]
                        for p in points:
                            p[th_key] = th_val

        # NB: the Vega-Lite spec is NOT stored on the KP — it roughly
        # duplicates `numbers` and would bloat the KB / distilled memory that
        # flows across turns. It is regenerated on demand at emit time in
        # `finalize._build_chart_payload`. See that helper for the rationale.

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
