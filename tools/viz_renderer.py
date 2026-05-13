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
    remains."""
    xs: list = []
    ys: list[float] = []
    for n in numbers:
        x = n.get(x_field)
        y = n.get(y_field)
        if x is None or y is None:
            continue
        try:
            ys.append(float(y))
        except (TypeError, ValueError):
            continue
        xs.append(x)
    if not xs:
        return None
    return xs, ys


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
                else:
                    # Grouped bars side-by-side per x value.
                    bar_w = 0.8 / n_series
                    for i, (yf, (_, ys)) in enumerate(extracted):
                        offsets = [x + (i - (n_series - 1) / 2) * bar_w for x in indices]
                        ax.bar(offsets, ys, width=bar_w * 0.95,
                               color=_PALETTE[i % len(_PALETTE)], label=yf)
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
                ax2 = ax.twinx()
                line2, = ax2.plot(indices, ys2, marker="s", linewidth=2.0,
                                  markersize=5.5, color=secondary_color, label=yf2)

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

    # Map our `kind` vocabulary to Vega-Lite marks. `share` → horizontal
    # bar (same as the matplotlib path); always single-series.
    if kind == "trend":
        spec["mark"] = "line"
        encoding: dict = {
            "x": {"field": x_field, "type": "ordinal"},
            "y": {"field": "value" if is_multi else primary_y,
                  "type": "quantitative"},
        }
        if is_multi:
            spec["transform"] = [{"fold": y_fields, "as": ["series", "value"]}]
            encoding["color"] = {"field": "series", "type": "nominal"}
        spec["encoding"] = encoding
    elif kind == "bar":
        spec["mark"] = "bar"
        encoding = {
            "x": {"field": x_field, "type": "ordinal"},
            "y": {"field": "value" if is_multi else primary_y,
                  "type": "quantitative"},
        }
        if is_multi:
            spec["transform"] = [{"fold": y_fields, "as": ["series", "value"]}]
            encoding["color"] = {"field": "series", "type": "nominal"}
            encoding["xOffset"] = {"field": "series", "type": "nominal"}
        spec["encoding"] = encoding
    else:  # share — horizontal, single-series only
        spec["mark"] = "bar"
        spec["encoding"] = {
            "y": {"field": x_field, "type": "ordinal",
                  "sort": {"field": primary_y, "order": "descending"}},
            "x": {"field": primary_y, "type": "quantitative"},
        }

    title = str(kp.get("topic") or "").replace("_", " ").strip()
    if title:
        spec["title"] = title
    return spec
