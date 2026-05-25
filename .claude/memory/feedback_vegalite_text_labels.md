---
name: feedback-vegalite-text-labels
description: Vega-Lite positioned text labels must use inline data.values + field encoding, not datum or SVG-native properties like stroke/paintOrder
metadata:
  type: feedback
---

When adding positioned text annotations (e.g., threshold labels) in Vega-Lite specs, use **inline `data.values`** with **field-based encoding**:

```json
{
  "data": {"values": [{"label": "threshold: 10", "y": 10}]},
  "mark": {"type": "text", "align": "left", "baseline": "bottom",
           "dx": 4, "dy": -4, "fontSize": 10, "color": "#3c4043"},
  "encoding": {
    "y": {"field": "y", "type": "quantitative"},
    "text": {"field": "label", "type": "nominal"}
  }
}
```

**Why:** Multiple approaches were tried and failed:
- `fontWeight: 600` on text marks → faux-bold doubling artifact
- `stroke: "white", strokeWidth: 3, paintOrder: "stroke"` → SVG-native properties not supported by Vega-Lite, text disappeared entirely
- `datum`-based encoding → worked for the rule mark but inconsistent for text positioning in dual-axis charts

The inline `data.values` approach is the only one that reliably renders across Vega-Lite renderers (the frontend prioritizes vega_spec over PNG).

**How to apply:** In `tools/viz_renderer.py`, `kp_to_vega_spec()` — any text annotation that needs to appear at a specific data position should use this pattern. Applied to both single-series (`trend`) and dual-axis (`trend_dual`) threshold labels.
