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


_SUPPORTED_KINDS = {"trend", "bar", "share"}

# Used to slugify topic names for filesystem safety. We keep alphanumerics +
# underscores; everything else collapses to a single underscore.
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")


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


def _resolve_axes(kp_viz: dict, numbers: list[dict]) -> tuple[str, str] | None:
    """Pick the x/y field names for the chart from the viz spec, falling back
    to common conventions when the spec omits them.

    For ``trend`` and ``bar`` we expect ``viz.x_field`` + ``viz.y_field``.
    For ``share`` (breakdowns) the convention from the distiller prompt is
    ``group`` / ``value``, but x_field/y_field can override.

    Returns None when neither the explicit fields nor the conventional
    fallbacks are present in the data.
    """
    x_field = kp_viz.get("x_field")
    y_field = kp_viz.get("y_field")

    sample = numbers[0]
    if x_field and y_field and x_field in sample and y_field in sample:
        return x_field, y_field

    # Fallbacks by convention (matches the distiller prompt examples).
    fallbacks_for_x = ("period", "group", "x")
    fallbacks_for_y = ("value", "y")

    if not x_field:
        x_field = next((f for f in fallbacks_for_x if f in sample), None)
    if not y_field:
        y_field = next((f for f in fallbacks_for_y if f in sample), None)

    if x_field and y_field and x_field in sample and y_field in sample:
        return x_field, y_field
    return None


def _extract_xy(numbers: list[dict], x_field: str, y_field: str
                ) -> tuple[list, list[float]] | None:
    """Build parallel x / y arrays. Drops entries with missing or
    non-numeric y values; returns None when nothing usable remains."""
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
    x_field, y_field = axes

    extracted = _extract_xy(numbers, x_field, y_field)
    if extracted is None:
        return None
    xs, ys = extracted

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

    try:
        fig, ax = plt.subplots(figsize=(7.5, 4.0), dpi=120)
        if kind == "trend":
            ax.plot(xs, ys, marker="o", linewidth=2)
            ax.set_xlabel(x_field)
            ax.set_ylabel(y_field)
            ax.grid(True, linestyle="--", alpha=0.4)
            # Tilt long x-tick labels (date strings) so they don't overlap.
            if any(isinstance(x, str) and len(x) > 4 for x in xs):
                fig.autofmt_xdate(rotation=30)
        elif kind == "bar":
            ax.bar(range(len(xs)), ys)
            ax.set_xticks(range(len(xs)))
            ax.set_xticklabels([str(x) for x in xs], rotation=30, ha="right")
            ax.set_xlabel(x_field)
            ax.set_ylabel(y_field)
            ax.grid(True, axis="y", linestyle="--", alpha=0.4)
        else:  # "share"
            # Horizontal bar — stable for 4-20 groups; pie charts get
            # unreadable past ~5 slices, so we standardize on hbar here.
            order = sorted(range(len(xs)), key=lambda i: ys[i], reverse=True)
            xs_sorted = [xs[i] for i in order]
            ys_sorted = [ys[i] for i in order]
            ax.barh(range(len(xs_sorted)), ys_sorted)
            ax.set_yticks(range(len(xs_sorted)))
            ax.set_yticklabels([str(x) for x in xs_sorted])
            ax.invert_yaxis()  # largest at top
            ax.set_xlabel(y_field)
            ax.set_ylabel(x_field)
            ax.grid(True, axis="x", linestyle="--", alpha=0.4)

        title = str(kp.get("topic") or "").replace("_", " ").strip()
        if title:
            ax.set_title(title)
        fig.tight_layout()
        fig.savefig(out_path, format="png", bbox_inches="tight")
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
                    "n_points": len(xs), "path": str(out_path)})
    return str(out_path)


def kp_to_vega_spec(kp: dict) -> dict | None:
    """Return a minimal Vega-Lite v5 spec for the KP, or None if the KP is
    not chart-able. The spec inlines ``numbers`` as ``data.values`` so
    a downstream renderer can produce the chart without going back to the
    original tool call.
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
    x_field, y_field = axes

    # Map our `kind` vocabulary to Vega-Lite marks. `share` → bar with the
    # axes flipped (matches the matplotlib horizontal-bar choice above).
    if kind == "trend":
        mark = "line"
        encoding = {
            "x": {"field": x_field, "type": "ordinal"},
            "y": {"field": y_field, "type": "quantitative"},
        }
    elif kind == "bar":
        mark = "bar"
        encoding = {
            "x": {"field": x_field, "type": "ordinal"},
            "y": {"field": y_field, "type": "quantitative"},
        }
    else:  # share
        mark = "bar"
        encoding = {
            "y": {"field": x_field, "type": "ordinal",
                  "sort": {"field": y_field, "order": "descending"}},
            "x": {"field": y_field, "type": "quantitative"},
        }

    title = str(kp.get("topic") or "").replace("_", " ").strip()
    spec: dict = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": numbers},
        "mark": mark,
        "encoding": encoding,
    }
    if title:
        spec["title"] = title
    return spec
