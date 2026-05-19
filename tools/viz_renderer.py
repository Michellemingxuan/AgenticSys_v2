"""Render KnowledgePoint viz specs to PNG charts + Vega-Lite specs.

Phase 2 of the memory rework (see ``tasks/prd-memory-management.md``).

Each :class:`models.types.KnowledgePoint` may carry a ``viz`` dict like
``{"kind": "trend"|"bar"|"share", "x_field": "period", "y_field": "value"}``
plus a ``numbers`` array of dicts (the data points). When a KP has both,
this module produces:

  * a static PNG written to ``reports/<case_id>/charts/<turn_id>-<topic>.png``
    for inline embedding in the agent's markdown answer, and
  * a minimal Vega-Lite v5 spec stored on the KP itself (``vega_spec``)
    so downstream tooling / future interactive frontends can re-render.

Failures (matplotlib import error on a stripped image, malformed numbers,
unrecognised kind, etc.) are logged but never raised — the caller treats
``None`` as "no chart this round; the prose answer still ships".
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Use the headless backend so this works inside the Flask server, in tests,
# and in any environment without a display. Must be set BEFORE pyplot import.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


_SUPPORTED_KINDS = {"trend", "bar", "share", "trend_dual", "trend_grid"}

# Used to slugify topic names for filesystem safety. We keep alphanumerics +
# underscores; everything else collapses to a single underscore.
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")

# Color palette — Amex-leaning blues + analyst-friendly accent colors. Used
# in series order so the first line/bar is the primary signal, the second a
# contrast, etc. Keep this short and meaningful; if we ever need more than 6
# series in one chart, the chart is probably the wrong tool.
_PALETTE = [
    "#006FCF",  # Amex blue (primary)
    "#E03C31",  # accent red
    "#00A287",  # accent teal
    "#F2A900",  # accent gold
    "#7A5195",  # accent purple
    "#666666",  # neutral gray
]


def _apply_style(ax, fig) -> None:
    """Apply the project's chart style to an Axes/Figure pair.

    Minimal-decoration look: no top/right spines, light gridlines, larger
    tick labels, generous figure margins. Centralized here so every chart
    in the system stays consistent without each render path repeating
    cosmetic configuration.
    """
    # Top + right spines off; left + bottom kept but desaturated.
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#9aa0a6")
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors="#3c4043", labelsize=10, length=4, width=0.8)
    # Soft horizontal gridlines on by default; the axis-specific render
    # paths below override (e.g. `bar` uses y-only).
    ax.grid(True, linestyle=":", linewidth=0.8, color="#dadce0", alpha=0.9)
    ax.set_axisbelow(True)
    ax.xaxis.label.set_color("#5f6368")
    ax.yaxis.label.set_color("#5f6368")
    ax.xaxis.label.set_fontsize(10)
    ax.yaxis.label.set_fontsize(10)
    # Slightly off-white background to hint at "report" rather than "raw plot".
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#fbfcfd")


def _slugify(text: str, max_len: int = 60) -> str:
    """Filesystem-safe slug from a topic name. Keeps the result short enough
    to avoid OS path-length limits on Windows mounts (the project lives on a
    Google Drive mount where deep paths can hit limits)."""
    cleaned = _SLUG_RE.sub("_", (text or "").strip()).strip("_")
    return (cleaned or "kp")[:max_len]


def _coerce_numbers(numbers: Any) -> list[dict] | None:
    """Validate the numbers array. Each entry must be a dict; non-dict
    entries skip the whole render (we won't render a half-bad series).
    """
    if not isinstance(numbers, list) or not numbers:
        return None
    if not all(isinstance(n, dict) for n in numbers):
        return None
    return numbers


# Date-like x-values get parsed via these formats (tried in order) so we
# can sort temporally. Anything that fails ALL parsers is treated as
# categorical and falls back to a lexicographic sort.
_DATE_FORMATS = (
    "%Y-%m-%d", "%Y-%m", "%Y/%m/%d", "%Y/%m",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
    "%m/%d/%Y", "%d-%m-%Y", "%d/%m/%Y",
    "%b %Y", "%B %Y", "%b-%y", "%b-%Y", "%Y",
)


def _parse_date_key(x: Any) -> Any:
    """Return a ``datetime`` if ``x`` parses as one of our date formats,
    otherwise None. Used as the temporal-sort key. Compact, format-by-
    format try-loop — matches the same pragma used in
    ``tools/data_tools.py:_date_key``."""
    if x is None:
        return None
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return None  # numbers aren't dates here; let other branches sort
    s = str(x).strip()
    if not s:
        return None
    from datetime import datetime
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _sort_points(
    points: list[dict],
    x_field: str,
    y_fields: list[str],
    kind: str,
) -> list[dict]:
    """Re-order the points array into a logical sequence for plotting.

    Priority:
      1. **Temporal** — if every x parses as a date, sort by date ascending.
      2. **Numeric x** — if every x is a number, sort numerically ascending.
      3. **Ranking** — for `bar` / `share` with categorical x and a SINGLE
         y_field, sort by y descending (top-N readability — biggest bar
         on the left for `bar`, on the top for `share`).
      4. **Alphabetic** — fallback, sort by str(x) ascending.

    Sort is stable; ties preserve the original order so multi-series
    `trend` charts keep their entries aligned across series.
    """
    if len(points) < 2:
        return list(points)

    xs = [p.get(x_field) for p in points]

    # (1) temporal
    date_keys = [_parse_date_key(x) for x in xs]
    if all(k is not None for k in date_keys):
        return [p for _, p in sorted(zip(date_keys, points), key=lambda pair: pair[0])]

    # (2) numeric
    def _as_float(x: Any) -> float | None:
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    nums = [_as_float(x) for x in xs]
    if all(n is not None for n in nums):
        return [p for _, p in sorted(zip(nums, points), key=lambda pair: pair[0])]

    # (3) ranking — bar/share single-series, sort by y desc
    if kind in ("bar", "share") and len(y_fields) == 1:
        yf = y_fields[0]

        def _y(p: dict) -> float:
            v = p.get(yf)
            try:
                return float(v)
            except (TypeError, ValueError):
                return float("-inf")

        return sorted(points, key=_y, reverse=True)

    # (4) alpha fallback
    return sorted(points, key=lambda p: str(p.get(x_field, "")))


def _resolve_axes(kp_viz: dict, numbers: list[dict]) -> tuple[str, list[str]] | None:
    """Resolve x_field + a LIST of y_fields. Multi-series charts (e.g.
    spend vs payment over time) plot one line per y_field on the same axes.

    Reads ``viz.x_field`` and either ``viz.y_fields`` (list, preferred) or
    ``viz.y_field`` (string, back-compat — wrapped to a single-element list).
    Falls back to convention names (``period`` / ``value``, ``group`` /
    ``value``) when the spec omits them.

    Returns None when neither the explicit fields nor the conventional
    fallbacks are present in the data.
    """
    x_field = kp_viz.get("x_field")
    y_fields_raw = kp_viz.get("y_fields")
    if y_fields_raw is None:
        # Back-compat: singular y_field still accepted; wrap.
        y_field = kp_viz.get("y_field")
        y_fields = [y_field] if y_field else []
    elif isinstance(y_fields_raw, list):
        y_fields = [f for f in y_fields_raw if isinstance(f, str)]
    elif isinstance(y_fields_raw, str):
        y_fields = [y_fields_raw]
    else:
        y_fields = []

    sample = numbers[0]

    # Fallbacks by convention.
    if not x_field:
        x_field = next((f for f in ("period", "group", "x") if f in sample), None)
    if not y_fields:
        fallback = next((f for f in ("value", "y") if f in sample), None)
        if fallback:
            y_fields = [fallback]

    if not x_field or x_field not in sample:
        return None
    y_fields = [f for f in y_fields if f in sample]
    if not y_fields:
        return None
    return x_field, y_fields


def _extract_xy(numbers: list[dict], x_field: str, y_field: str
                ) -> tuple[list, list[float]] | None:
    """Build parallel x / y arrays for a single y series. Drops entries with
    missing or non-numeric y values; returns None when nothing usable
    remains.

    Note: ``float("NaN")`` doesn't raise — it returns the IEEE NaN value.
    We treat NaN / ±inf as "unparseable" too because matplotlib's
    annotate call chokes on non-finite xy coordinates, and a chart with
    a NaN point reads as broken to a reviewer regardless.
    """
    import math
    xs: list = []
    ys: list[float] = []
    for n in numbers:
        x = n.get(x_field)
        y = n.get(y_field)
        if x is None or y is None:
            continue
        try:
            fy = float(y)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(fy):
            continue
        ys.append(fy)
        xs.append(x)
    if not xs:
        return None
    return xs, ys


def _consistent_threshold(numbers: list[dict]) -> float | None:
    """If every entry in ``numbers`` has the same finite numeric
    ``threshold`` value, return it; otherwise None.

    The distiller's KP shape allows a per-row ``threshold`` key on
    threshold-breach claims (e.g. *"`times_30_dpd` crossed risky
    threshold > 1 in 2024-Q4"*). When the threshold is constant across
    the whole series — the common case for catalog-defined risky cutoffs
    like "Values above 0.5 are risky" — we draw a single horizontal
    reference line on the chart. Per-row varying thresholds (rare) skip
    rendering to avoid a misleading step-function overlay.
    """
    return _read_consistent_key(numbers, "threshold")


def _per_field_threshold(numbers: list[dict], y_field: str) -> float | None:
    """Per-axis threshold lookup for multi-y / dual charts.

    Reads `threshold_<y_field>` (e.g. `threshold_credit_loss_prob`) so
    each y-axis can carry its own catalog-defined risky cutoff — `CDSS`
    at 0.5 on a 0-1 probability axis, `TSR` at 20 on a 0-100 score axis.
    The distiller emits these per-field keys when the source schema's
    descriptions named them. Same consistency rule as
    `_consistent_threshold` — returns None if rows disagree or any row
    lacks the key.
    """
    return _read_consistent_key(numbers, f"threshold_{y_field}")


def _read_consistent_key(numbers: list[dict], key: str) -> float | None:
    """Shared helper: return the single shared finite numeric value of
    ``key`` across all rows, or None if any row is missing it / disagrees
    / non-numeric. Used by both ``_consistent_threshold`` and
    ``_per_field_threshold``.
    """
    import math
    seen: float | None = None
    for n in numbers:
        if not isinstance(n, dict):
            continue
        t = n.get(key)
        if t is None:
            return None
        try:
            ft = float(t)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(ft):
            return None
        if seen is None:
            seen = ft
        elif seen != ft:
            return None
    return seen


def _align_multi_series_points(
    numbers: list[dict], x_field: str, y_fields: list[str],
) -> list[dict]:
    """Filter ``numbers`` to entries where x is present AND EVERY y_field
    has a finite numeric value.

    Why this exists: multi-series chart kinds (trend with 2+ y_fields,
    trend_dual, trend_grid) plot one line per y_field on a SHARED x-axis.
    Per-series ``_extract_xy`` drops entries with a missing y, but it
    does so independently per series — so if one period has CDSS but a
    NaN TSR (or vice versa), the two extracted arrays come out at
    different lengths and matplotlib raises ``ValueError: x and y must
    have same first dimension``. Real failure case: case-aefd66 turn
    `5b8f94089581`, topic `cdss_tsr_trajectory` — shapes (4,) and (5,).

    Pre-filtering to common-valid rows up front guarantees every
    downstream ``_extract_xy`` produces same-length arrays. Single-
    series kinds (bar / share, or trend with 1 y_field) don't call
    this — their per-series extraction is independent by design.
    """
    import math
    aligned: list[dict] = []
    for n in numbers:
        if n.get(x_field) is None:
            continue
        ok = True
        for yf in y_fields:
            v = n.get(yf)
            if v is None:
                ok = False
                break
            try:
                fv = float(v)
            except (TypeError, ValueError):
                ok = False
                break
            if not math.isfinite(fv):
                ok = False
                break
        if ok:
            aligned.append(n)
    return aligned


def _format_axis_value(v: float) -> str:
    """Compact human-readable label for tick values. $12,500 → '$12.5K',
    1.2e6 → '1.2M', etc. Used on y-axis tick labels for trend/bar so big
    money values don't blow out the chart margins."""
    av = abs(v)
    if av >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if av >= 1_000:
        return f"{v / 1_000:.1f}K"
    if av == int(av):
        return f"{int(v)}"
    return f"{v:.2f}"


def _annotate_points(ax, xs, ys, color: str, fontsize: int = 8) -> None:
    """Write each y value as a small label just above its data point so
    the reviewer can read exact figures off the chart without
    cross-referencing the y-axis ticks.

    Offset is in display pixels (8px above the marker) so dense series
    don't push labels off the top of the chart at low DPI. Skip labels
    that would land too close to the previous one (3% of the x-range
    threshold) — only relevant when the chart has dozens of points and
    they bunch up.
    """
    if not ys:
        return
    last_x: float | None = None
    span = (max(xs) - min(xs)) if len(xs) > 1 else 1.0
    min_gap = max(span * 0.03, 0)
    for x, y in zip(xs, ys):
        if last_x is not None and (x - last_x) < min_gap:
            continue
        ax.annotate(
            _format_axis_value(y),
            xy=(x, y),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center", va="bottom",
            fontsize=fontsize,
            color=color,
            fontweight="600",
        )
        last_x = x


def _annotate_bars(ax, xs, ys, color: str, fontsize: int = 9) -> None:
    """Write each bar's value just above the top of the bar. Centered
    horizontally on the bar; offset 4px vertically so the text reads as
    a label above the bar rather than overlapping it.
    """
    for x, y in zip(xs, ys):
        if y is None:
            continue
        ax.annotate(
            _format_axis_value(y),
            xy=(x, y),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center", va="bottom",
            fontsize=fontsize,
            color=color,
            fontweight="600",
        )


def render_chart(
    kp: dict,
    out_dir: Path,
    *,
    turn_id: str | None = None,
    logger: Any = None,
) -> str | None:
    """Render a KnowledgePoint to a PNG. Returns the absolute output path
    as a string, or None on any failure.

    The caller (``redacting_tool._distill_and_persist``) decides what to do
    with the path (typically: store on the KP and let server.py turn it
    into a markdown image link in the agent's answer).
    """
    viz = kp.get("viz") if isinstance(kp, dict) else None
    if not isinstance(viz, dict):
        return None
    kind = viz.get("kind")
    if kind not in _SUPPORTED_KINDS:
        if logger is not None:
            logger.log("viz_render_skipped",
                       {"reason": "unsupported_kind", "kind": kind,
                        "topic": kp.get("topic")})
        return None

    numbers = _coerce_numbers(kp.get("numbers"))
    if numbers is None:
        return None

    axes = _resolve_axes(viz, numbers)
    if axes is None:
        if logger is not None:
            logger.log("viz_render_skipped",
                       {"reason": "axes_unresolved",
                        "topic": kp.get("topic"),
                        "viz": viz,
                        "sample_keys": list(numbers[0].keys())})
        return None
    x_field, y_fields = axes

    # Sort the points into a logical order BEFORE extracting parallel
    # arrays. For temporal x → chronological; for ranking-style charts
    # → biggest bar first; else alpha. Without this, an unsorted points
    # array drawn into a `trend` chart can produce nonsensical
    # back-and-forth line segments when the specialist's tool calls
    # returned data in non-chronological order.
    numbers = _sort_points(numbers, x_field, y_fields, kind)

    # Multi-series alignment: when 2+ y_fields share the same x-axis
    # (multi-y trend, trend_dual, trend_grid), pre-filter `numbers` to
    # rows where every RESOLVABLE y_field is finite-numeric. Without
    # alignment, a missing/NaN entry on one series produces shorter ys
    # arrays on different series and matplotlib raises ValueError
    # (case-aefd66 turn `5b8f94089581`, topic `cdss_tsr_trajectory`).
    #
    # First, drop y_fields that have ZERO numeric values across the
    # whole series — those series wouldn't render anyway, and forcing
    # alignment against them would empty `numbers`. This preserves the
    # "drop unparseable series silently" contract from the pre-fix
    # behavior. Single-series kinds (single-y trend, bar, share) bypass
    # the whole block — only one series to extract.
    if len(y_fields) > 1 or kind in ("trend_dual", "trend_grid"):
        def _has_any_numeric(yf: str) -> bool:
            import math as _m
            for n in numbers:
                v = n.get(yf)
                if v is None:
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if _m.isfinite(fv):
                    return True
            return False

        resolvable = [yf for yf in y_fields if _has_any_numeric(yf)]
        if not resolvable:
            if logger is not None:
                logger.log("viz_render_skipped",
                           {"reason": "no_resolvable_y_fields",
                            "topic": kp.get("topic"),
                            "x_field": x_field,
                            "y_fields": y_fields})
            return None
        y_fields = resolvable
        numbers = _align_multi_series_points(numbers, x_field, y_fields)
        if not numbers:
            if logger is not None:
                logger.log("viz_render_skipped",
                           {"reason": "no_aligned_points",
                            "topic": kp.get("topic"),
                            "x_field": x_field,
                            "y_fields": y_fields,
                            "note": ("no entries had finite values for all "
                                     "resolvable y_fields; cannot align "
                                     "multi-series.")})
            return None

    # One (xs, ys) pair per y_field — supports multi-series trend/bar.
    extracted = []
    for yf in y_fields:
        out = _extract_xy(numbers, x_field, yf)
        if out is None:
            continue
        extracted.append((yf, out))
    if not extracted:
        return None

    # Filename: <turn>-<topic>.png. When turn_id is missing, fall back to
    # the captured_at_turn already on the KP, then to a random short id so
    # we never overwrite a sibling unintentionally.
    topic = _slugify(str(kp.get("topic") or "kp"))
    tid = str(turn_id or kp.get("captured_at_turn") or "untagged")
    tid = _slugify(tid)
    filename = f"{tid}-{topic}.png"

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        if logger is not None:
            logger.log("viz_render_failed",
                       {"reason": "mkdir_failed",
                        "topic": kp.get("topic"),
                        "out_dir": str(out_dir),
                        "error": str(exc)})
        return None

    # Defense-in-depth: if a sibling KP this turn already wrote to this
    # filename (same <turn>-<topic> slug — distiller misclassified two
    # distinct metrics under one topic), suffix the new file with a
    # counter so we don't silently overwrite the earlier render. The
    # downstream `_collect_turn_charts` still dedupes by topic — see
    # caller comments — so the new filename also surfaces a side-by-
    # side render only when the KB carries distinct topics. The PNG
    # preservation here is for forensics: when the distiller bug
    # recurs, the missing chart's PNG is still on disk to recover.
    out_dir_path = out_dir
    candidate = out_dir_path / filename
    suffix = 1
    while candidate.exists():
        suffix += 1
        filename = f"{tid}-{topic}__dup{suffix}.png"
        candidate = out_dir_path / filename
        if suffix > 9:  # paranoia cap: never write more than 8 dupes
            break
    if suffix > 1 and logger is not None:
        logger.log("viz_render_filename_collision", {
            "topic": kp.get("topic"),
            "turn_id": turn_id,
            "resolved_filename": filename,
            "collision_index": suffix,
        })

    out_path = out_dir / filename
    is_multi = len(extracted) > 1
    # No title baked into the PNG — the surrounding UI (chart-button label
    # + lightbox header) already shows the topic, so a chart title would be
    # redundant ("double titles" the user flagged). Keep the chart visually
    # clean and let the UI provide the framing.
    y_label = ", ".join(yf for yf, _ in extracted) if is_multi else extracted[0][0]

    try:
        if kind == "trend_grid":
            # One panel per resolved series, vertical stack, shared x-axis.
            n_panels = len(extracted)
            fig, axes = plt.subplots(
                n_panels, 1, sharex=True,
                figsize=(8.5, 2.2 * n_panels), dpi=140,
            )
            # plt.subplots returns a single Axes when n_panels == 1; wrap
            # to a list so the per-panel loop below is uniform.
            if n_panels == 1:
                axes = [axes]

            xs_first = extracted[0][1][0]
            indices = list(range(len(xs_first)))
            n = len(xs_first)
            stride = max(1, n // 10)
            visible_xticklabels = [
                str(xs_first[i]) if (i % stride == 0 or i == n - 1) else ""
                for i in indices
            ]

            for i, (yf, (_, ys)) in enumerate(extracted):
                panel_ax = axes[i]
                color = _PALETTE[i % len(_PALETTE)]
                panel_ax.plot(indices, ys, marker="o", linewidth=2.0,
                              markersize=5.0, color=color)
                _annotate_points(panel_ax, indices, ys, color)
                panel_ax.set_ylabel(yf)
                panel_ax.yaxis.set_major_formatter(
                    plt.FuncFormatter(lambda v, _p: _format_axis_value(v)))
                _apply_style(panel_ax, fig)
                # Hide x-tick labels on every panel except the bottom one
                # so the shared x is only labelled once at the foot.
                if i < n_panels - 1:
                    panel_ax.tick_params(labelbottom=False)

            # Bottom panel gets the rotated x-tick labels + axis label.
            bottom_ax = axes[-1]
            bottom_ax.set_xticks(indices)
            bottom_ax.set_xticklabels(visible_xticklabels, rotation=30,
                                      ha="right", fontsize=9)
            bottom_ax.set_xlabel(x_field)

            # Keep panels visually distinct but tight.
            fig.tight_layout(h_pad=0.6)
            fig.savefig(out_path, format="png", bbox_inches="tight",
                        facecolor=fig.get_facecolor())
        else:
            fig, ax = plt.subplots(figsize=(8.5, 4.5), dpi=140)

            if kind == "trend":
                xs_first = extracted[0][1][0]
                # Pin every data point as an x-tick so the rendered axis shows
                # the full range from the first to the last entry — fixes the
                # "claim says 2024-11..2025-07 but axis shows fewer months"
                # mismatch the reviewer hits when matplotlib auto-thins ticks.
                indices = list(range(len(xs_first)))
                for i, (yf, (_, ys)) in enumerate(extracted):
                    color = _PALETTE[i % len(_PALETTE)]
                    ax.plot(indices, ys, marker="o", linewidth=2.0, markersize=5.5,
                            color=color, label=yf if is_multi else None)
                    # Annotate each point with its exact value so the reviewer
                    # can read figures off the chart without cross-referencing
                    # the y-axis ticks.
                    _annotate_points(ax, indices, ys, color)
                # Threshold reference line — when every `numbers` entry
                # carries the same finite `threshold` value (e.g. catalog
                # "Values above 0.5 are risky"), draw a dashed horizontal
                # at that y plus an end-of-line label so the reader can
                # see at a glance which points breach. Skipped for
                # multi-series same-scale trends where the per-series
                # threshold might differ but only a single key is given
                # — unsafe to apply one threshold across mixed metrics.
                threshold = (
                    _consistent_threshold(numbers) if not is_multi else None
                )
                if threshold is not None:
                    ax.axhline(threshold, color="#666666", linestyle="--",
                               linewidth=1.0, alpha=0.85, zorder=0)
                    ax.text(
                        len(indices) - 1, threshold,
                        f"  threshold: {_format_axis_value(threshold)}",
                        va="center", ha="left", fontsize=9,
                        color="#3c4043", fontweight="600",
                    )
                ax.set_xticks(indices)
                # Thin to ~10 visible labels max so dense series stay readable
                # without dropping the first / last (those are anchor points).
                n = len(xs_first)
                stride = max(1, n // 10)
                visible = [str(xs_first[i]) if (i % stride == 0 or i == n - 1) else ""
                           for i in indices]
                ax.set_xticklabels(visible, rotation=30, ha="right", fontsize=9)
                ax.set_xlabel(x_field)
                ax.set_ylabel(y_label)
                if is_multi:
                    ax.legend(loc="best", frameon=False, fontsize=9)
                ax.yaxis.set_major_formatter(
                    plt.FuncFormatter(lambda v, _p: _format_axis_value(v))
                )

            elif kind == "bar":
                xs_first = extracted[0][1][0]
                n_groups = len(xs_first)
                n_series = len(extracted)
                indices = list(range(n_groups))
                if n_series == 1:
                    yf, (_, ys) = extracted[0]
                    ax.bar(indices, ys, color=_PALETTE[0], width=0.6)
                    _annotate_bars(ax, indices, ys, _PALETTE[0])
                else:
                    # Grouped bars side-by-side per x value.
                    bar_w = 0.8 / n_series
                    for i, (yf, (_, ys)) in enumerate(extracted):
                        offsets = [x + (i - (n_series - 1) / 2) * bar_w for x in indices]
                        color = _PALETTE[i % len(_PALETTE)]
                        ax.bar(offsets, ys, width=bar_w * 0.95,
                               color=color, label=yf)
                        _annotate_bars(ax, offsets, ys, color, fontsize=8)
                    ax.legend(loc="best", frameon=False, fontsize=9)
                ax.set_xticks(indices)
                ax.set_xticklabels([str(x) for x in xs_first], rotation=30,
                                   ha="right", fontsize=9)
                ax.set_xlabel(x_field)
                ax.set_ylabel(y_label)
                ax.grid(True, axis="y", linestyle=":", linewidth=0.8,
                        color="#dadce0", alpha=0.9)
                ax.grid(False, axis="x")
                ax.yaxis.set_major_formatter(
                    plt.FuncFormatter(lambda v, _p: _format_axis_value(v))
                )

            elif kind == "trend_dual":
                # Two series on twin y-axes. trend_dual ENFORCES that both
                # extracted series exist and align on the same x — if one is
                # missing or all-unparseable, _extract_xy returned None and the
                # extracted list is shorter than 2; bail to None so we don't
                # silently mislabel a 1-line chart as `trend_dual`.
                if len(extracted) != 2:
                    if logger is not None:
                        logger.log("viz_render_skipped",
                                   {"reason": "trend_dual_needs_two_series",
                                    "topic": kp.get("topic"),
                                    "n_resolved": len(extracted)})
                    try:
                        plt.close(fig)
                    except Exception:
                        pass
                    return None

                (yf1, (xs_first, ys1)) = extracted[0]
                (yf2, (_, ys2)) = extracted[1]
                indices = list(range(len(xs_first)))

                primary_color = _PALETTE[0]
                secondary_color = _PALETTE[1]

                line1, = ax.plot(indices, ys1, marker="o", linewidth=2.0,
                                 markersize=5.5, color=primary_color, label=yf1)
                _annotate_points(ax, indices, ys1, primary_color)
                ax2 = ax.twinx()
                line2, = ax2.plot(indices, ys2, marker="s", linewidth=2.0,
                                  markersize=5.5, color=secondary_color, label=yf2)
                _annotate_points(ax2, indices, ys2, secondary_color)

                ax.set_xticks(indices)
                n = len(xs_first)
                stride = max(1, n // 10)
                visible = [str(xs_first[i]) if (i % stride == 0 or i == n - 1) else ""
                           for i in indices]
                ax.set_xticklabels(visible, rotation=30, ha="right", fontsize=9)
                ax.set_xlabel(x_field)

                # Label each y-axis with its field name, color-matched to the
                # corresponding line so the reader maps line→axis at a glance.
                ax.set_ylabel(yf1, color=primary_color)
                ax2.set_ylabel(yf2, color=secondary_color)
                ax.tick_params(axis="y", colors=primary_color)
                ax2.tick_params(axis="y", colors=secondary_color)

                # Compact value formatting on both axes.
                ax.yaxis.set_major_formatter(
                    plt.FuncFormatter(lambda v, _p: _format_axis_value(v)))
                ax2.yaxis.set_major_formatter(
                    plt.FuncFormatter(lambda v, _p: _format_axis_value(v)))

                # Combined legend — both lines named in one box.
                ax.legend(handles=[line1, line2], loc="best",
                          frameon=False, fontsize=9)

                # Hide the twin axis's top/right spines for a clean look.
                ax2.spines["top"].set_visible(False)
                ax2.spines["right"].set_color("#9aa0a6")
                ax2.spines["right"].set_linewidth(0.8)

                # Per-axis threshold reference lines. Read
                # `threshold_<y_field>` from each row (e.g.
                # `threshold_credit_loss_prob` for the left line,
                # `threshold_tot_struct_risk_score` for the right). Each
                # axis has its own scale, so the lines are drawn on the
                # axis that matches their series' y-field. Skipped per-
                # series when the data lacks a consistent threshold.
                t_left = _per_field_threshold(numbers, yf1)
                if t_left is not None:
                    ax.axhline(t_left, color=primary_color, linestyle="--",
                               linewidth=1.0, alpha=0.7, zorder=0)
                    ax.text(
                        len(indices) - 1, t_left,
                        f"  {_format_axis_value(t_left)}",
                        va="center", ha="left", fontsize=8,
                        color=primary_color, fontweight="600",
                    )
                t_right = _per_field_threshold(numbers, yf2)
                if t_right is not None:
                    ax2.axhline(t_right, color=secondary_color, linestyle="--",
                                linewidth=1.0, alpha=0.7, zorder=0)
                    ax2.text(
                        0, t_right,
                        f"{_format_axis_value(t_right)}  ",
                        va="center", ha="right", fontsize=8,
                        color=secondary_color, fontweight="600",
                    )

            else:  # "share" — horizontal bar, single series only
                yf, (xs, ys) = extracted[0]
                order = sorted(range(len(xs)), key=lambda i: ys[i], reverse=True)
                xs_sorted = [xs[i] for i in order]
                ys_sorted = [ys[i] for i in order]
                bars = ax.barh(range(len(xs_sorted)), ys_sorted,
                               color=_PALETTE[0], height=0.65)
                # Inline value labels at the end of each bar — much easier to
                # read than squinting at the x-axis on dense breakdowns.
                max_y = max(ys_sorted) if ys_sorted else 1
                for bar, v in zip(bars, ys_sorted):
                    ax.text(bar.get_width() + max_y * 0.01,
                            bar.get_y() + bar.get_height() / 2,
                            _format_axis_value(v),
                            va="center", ha="left", fontsize=9, color="#3c4043")
                ax.set_yticks(range(len(xs_sorted)))
                ax.set_yticklabels([str(x) for x in xs_sorted], fontsize=9)
                ax.invert_yaxis()
                ax.set_xlabel(yf)
                ax.set_ylabel("")
                ax.grid(True, axis="x", linestyle=":", linewidth=0.8,
                        color="#dadce0", alpha=0.9)
                ax.grid(False, axis="y")
                ax.xaxis.set_major_formatter(
                    plt.FuncFormatter(lambda v, _p: _format_axis_value(v))
                )
                # Pad right margin for the value labels.
                ax.set_xlim(right=max_y * 1.18)

            _apply_style(ax, fig)
            fig.tight_layout()
            fig.savefig(out_path, format="png", bbox_inches="tight",
                        facecolor=fig.get_facecolor())
    except Exception as exc:  # noqa: BLE001
        if logger is not None:
            logger.log("viz_render_failed",
                       {"reason": "matplotlib_error",
                        "topic": kp.get("topic"),
                        "kind": kind,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:300]})
        try:
            plt.close(fig)
        except Exception:
            pass
        return None
    finally:
        try:
            plt.close(fig)
        except Exception:
            pass

    if logger is not None:
        logger.log("viz_rendered",
                   {"topic": kp.get("topic"), "kind": kind,
                    "n_series": len(extracted),
                    "n_points": len(extracted[0][1][0]),
                    "path": str(out_path)})
    return str(out_path)


def kp_to_vega_spec(kp: dict) -> dict | None:
    """Return a minimal Vega-Lite v5 spec for the KP, or None if the KP is
    not chart-able. The spec inlines ``numbers`` as ``data.values`` so
    a downstream renderer can produce the chart without going back to the
    original tool call.

    For multi-series KPs (``viz.y_fields`` with 2+ entries), the spec uses
    Vega-Lite's ``transform: [{fold: y_fields}]`` to long-form the data and
    color-encodes by the resulting ``key`` channel — same numbers, but the
    chart shows N lines / grouped bars instead of one.
    """
    viz = kp.get("viz") if isinstance(kp, dict) else None
    if not isinstance(viz, dict):
        return None
    kind = viz.get("kind")
    if kind not in _SUPPORTED_KINDS:
        return None
    numbers = _coerce_numbers(kp.get("numbers"))
    if numbers is None:
        return None
    axes = _resolve_axes(viz, numbers)
    if axes is None:
        return None
    x_field, y_fields = axes
    is_multi = len(y_fields) > 1
    primary_y = y_fields[0]

    spec: dict = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": numbers},
    }

    # Vega-Lite number format for value labels. ".3~s" gives ~3
    # significant figures with SI suffix and strips trailing zeros:
    #   1200 → "1.2k", 12000 → "12k", 1_234_567 → "1.23M". Same idea as
    # `_format_axis_value` on the matplotlib path, so the PNG and the
    # interactive SVG read consistently.
    _LABEL_FMT = ".3~s"

    # Mark policy: single-series kinds keep inline `text` value labels
    # (low collision risk, reads at a glance). Multi-series kinds drop
    # the static text labels and use `tooltip` encoding so the reviewer
    # sees the exact number on HOVER — avoids the overlapping-labels
    # mess that real cases produced when two lines + their value
    # annotations crowded the same x positions (case-aefd66 turn
    # `5b8f94089581` cdss_tsr_trajectory chart). Legend placement on
    # multi-series goes `top` so the chart body uses the full width
    # instead of giving 20% of horizontal real estate to a sidebar.
    if kind == "trend":
        y_value_field = "value" if is_multi else primary_y
        line_enc: dict = {
            "x": {"field": x_field, "type": "ordinal"},
            "y": {"field": y_value_field, "type": "quantitative"},
            "tooltip": [
                {"field": x_field, "type": "ordinal"},
                {"field": y_value_field, "type": "quantitative",
                 "format": _LABEL_FMT},
            ],
        }
        if is_multi:
            spec["transform"] = [{"fold": y_fields, "as": ["series", "value"]}]
            line_enc["color"] = {"field": "series", "type": "nominal",
                                 "legend": {"orient": "top"}}
            # In multi-series, the tooltip also names which series the
            # hovered point belongs to.
            line_enc["tooltip"].insert(0, {"field": "series", "type": "nominal"})
            spec["layer"] = [
                {"mark": {"type": "line", "point": True}, "encoding": line_enc},
            ]
        else:
            # Single-series: keep the inline value labels — no overlap.
            text_enc: dict = {
                "x": {"field": x_field, "type": "ordinal"},
                "y": {"field": y_value_field, "type": "quantitative"},
                "text": {"field": y_value_field, "type": "quantitative",
                         "format": _LABEL_FMT},
            }
            spec["layer"] = [
                {"mark": {"type": "line", "point": True}, "encoding": line_enc},
                {"mark": {"type": "text", "dy": -10, "fontSize": 10,
                          "fontWeight": 600}, "encoding": text_enc},
            ]
            # Threshold reference line — same data shape as the matplotlib
            # path (every `numbers` row carries a constant `threshold`).
            # Render as a `rule` mark at the threshold y plus a `text`
            # mark labeling it at the right edge of the chart. Single-
            # series only; multi-series same-scale trends skip this
            # because one threshold key against mixed metrics is unsafe.
            t = _consistent_threshold(numbers)
            if t is not None:
                spec["layer"].append({
                    "mark": {"type": "rule", "strokeDash": [4, 3],
                             "color": "#666666", "opacity": 0.85},
                    "encoding": {
                        "y": {"datum": t, "type": "quantitative"},
                    },
                })
                spec["layer"].append({
                    "mark": {"type": "text", "align": "right", "baseline": "bottom",
                             "dx": -4, "dy": -2, "fontSize": 10,
                             "fontWeight": 600, "color": "#3c4043"},
                    "encoding": {
                        "y": {"datum": t, "type": "quantitative"},
                        "text": {"value": f"threshold: {t}"},
                    },
                })
    elif kind == "bar":
        y_value_field = "value" if is_multi else primary_y
        bar_enc: dict = {
            "x": {"field": x_field, "type": "ordinal"},
            "y": {"field": y_value_field, "type": "quantitative"},
            "tooltip": [
                {"field": x_field, "type": "ordinal"},
                {"field": y_value_field, "type": "quantitative",
                 "format": _LABEL_FMT},
            ],
        }
        if is_multi:
            spec["transform"] = [{"fold": y_fields, "as": ["series", "value"]}]
            bar_enc["color"] = {"field": "series", "type": "nominal",
                                "legend": {"orient": "top"}}
            bar_enc["xOffset"] = {"field": "series", "type": "nominal"}
            bar_enc["tooltip"].insert(0, {"field": "series", "type": "nominal"})
            spec["layer"] = [{"mark": "bar", "encoding": bar_enc}]
        else:
            # Single-series bar: keep inline value labels (no overlap).
            text_enc = {
                "x": {"field": x_field, "type": "ordinal"},
                "y": {"field": y_value_field, "type": "quantitative"},
                "text": {"field": y_value_field, "type": "quantitative",
                         "format": _LABEL_FMT},
            }
            spec["layer"] = [
                {"mark": "bar", "encoding": bar_enc},
                {"mark": {"type": "text", "dy": -6, "fontSize": 10,
                          "fontWeight": 600}, "encoding": text_enc},
            ]
    elif kind == "trend_dual":
        # Two line marks, each bound to its own y_field, with independent
        # y scales (`resolve.scale.y = independent`). The SECOND axis is
        # explicitly `orient='right'` so the two y-axes show one on each
        # side of the chart — pre-fix they both rendered on the left,
        # overlapping each other's title (case-aefd66 chart). Tooltip on
        # hover replaces the static text labels which used to overlap
        # between the two lines.
        y_left, y_right = y_fields[0], y_fields[1]

        def _dual_axis_group(
            y_field: str, axis_orient: str, threshold: float | None,
        ) -> dict:
            """Build one nested-layer group: a line + its optional
            threshold rule. The inner layers default to shared y-scale,
            so the rule's `y.datum` is interpreted in the same scale as
            the line's `y.field` — crucial when the outer composition
            uses `resolve.scale.y = independent` (rule with datum would
            otherwise get its own micro-scale and render at the wrong
            position). Color and legend stay on the outer line mark."""
            inner: list[dict] = [
                {
                    "mark": {"type": "line", "point": True},
                    "encoding": {
                        "x": {"field": x_field, "type": "ordinal"},
                        "y": {"field": y_field, "type": "quantitative",
                              "axis": {"orient": axis_orient,
                                       "title": y_field}},
                        "color": {"datum": y_field, "type": "nominal",
                                  "legend": {"orient": "top",
                                             "title": None}},
                        "tooltip": [
                            {"field": x_field, "type": "ordinal"},
                            {"field": y_field, "type": "quantitative",
                             "format": _LABEL_FMT, "title": y_field},
                        ],
                    },
                },
            ]
            if threshold is not None:
                # Rule + label on the SAME nested-layer scale as the
                # line. Color matches the series so the reviewer ties
                # the threshold to the right axis at a glance.
                inner.append({
                    "mark": {"type": "rule", "strokeDash": [4, 3],
                             "opacity": 0.75},
                    "encoding": {
                        "y": {"datum": threshold, "type": "quantitative"},
                        "color": {"datum": y_field, "type": "nominal"},
                    },
                })
                inner.append({
                    "mark": {"type": "text",
                             "align": "right" if axis_orient == "right" else "left",
                             "baseline": "bottom",
                             "dx": -4 if axis_orient == "right" else 4,
                             "dy": -2, "fontSize": 9, "fontWeight": 600},
                    "encoding": {
                        "y": {"datum": threshold, "type": "quantitative"},
                        "text": {"value": f"{y_field}: {threshold}"},
                        "color": {"datum": y_field, "type": "nominal"},
                    },
                })
            return {"layer": inner}

        t_left = _per_field_threshold(numbers, y_left)
        t_right = _per_field_threshold(numbers, y_right)
        spec["layer"] = [
            _dual_axis_group(y_left, "left", t_left),
            _dual_axis_group(y_right, "right", t_right),
        ]
        spec["resolve"] = {"scale": {"y": "independent"}}
    elif kind == "trend_grid":
        # Stacked single-series line charts sharing the x-axis. One
        # series per panel — no legend needed (the y-axis title IS the
        # series name). Inline value labels are kept because each panel
        # has only one line; no within-panel overlap.
        spec["vconcat"] = [
            {
                "layer": [
                    {
                        "mark": {"type": "line", "point": True},
                        "encoding": {
                            "x": {"field": x_field, "type": "ordinal"},
                            "y": {"field": yf, "type": "quantitative"},
                            "tooltip": [
                                {"field": x_field, "type": "ordinal"},
                                {"field": yf, "type": "quantitative",
                                 "format": _LABEL_FMT, "title": yf},
                            ],
                        },
                    },
                    {
                        "mark": {"type": "text", "dy": -10, "fontSize": 10,
                                 "fontWeight": 600},
                        "encoding": {
                            "x": {"field": x_field, "type": "ordinal"},
                            "y": {"field": yf, "type": "quantitative"},
                            "text": {"field": yf, "type": "quantitative",
                                     "format": _LABEL_FMT},
                        },
                    },
                ],
            }
            for yf in y_fields
        ]
    else:  # share — horizontal, single-series only
        spec["layer"] = [
            {
                "mark": "bar",
                "encoding": {
                    "y": {"field": x_field, "type": "ordinal",
                          "sort": {"field": primary_y, "order": "descending"}},
                    "x": {"field": primary_y, "type": "quantitative"},
                    "tooltip": [
                        {"field": x_field, "type": "ordinal"},
                        {"field": primary_y, "type": "quantitative",
                         "format": _LABEL_FMT},
                    ],
                },
            },
            {
                # Value labels at the end of each horizontal bar (dx=4,
                # align=left). Mirrors the matplotlib `share` branch
                # that inlines labels just past the bar end.
                "mark": {"type": "text", "dx": 4, "align": "left",
                         "fontSize": 10, "fontWeight": 600},
                "encoding": {
                    "y": {"field": x_field, "type": "ordinal",
                          "sort": {"field": primary_y, "order": "descending"}},
                    "x": {"field": primary_y, "type": "quantitative"},
                    "text": {"field": primary_y, "type": "quantitative",
                             "format": _LABEL_FMT},
                },
            },
        ]

    title = str(kp.get("topic") or "").replace("_", " ").strip()
    if title:
        spec["title"] = title
    return spec
