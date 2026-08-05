"""Palette and Plotly templates for the Dash app (Phase 7).

Every colour here is validated rather than chosen by eye. The categorical
slots clear the lightness band, chroma floor, colour-vision-deficiency
separation, normal-vision separation and surface contrast in **both** modes,
checked with the dataviz skill's validator on the all-pairs list:

* light — worst CVD ΔE 9.2, worst normal-vision ΔE 24.0
* dark  — worst CVD ΔE 9.4, worst normal-vision ΔE 20.9

Two rules fall out of that and are load-bearing rather than stylistic:

**Three categorical slots, not eight.** Past three, no ordering of the source
palette clears the all-pairs floors — the fourth slot puts yellow beside
orange, which fails. Nothing in this app needs more than three series, and a
chart that would is a chart that should be faceted.

**Dark is a separate set of steps, not an inversion.** Flipping a light
palette onto a dark surface loses the contrast guarantee, so the dark column
is the same three hues re-stepped for the dark surface and validated against
it independently.

One accepted warning: on the light surface, aqua sits at 2.74:1 — below the
3:1 bar. It is therefore never used as a light-mode fill that has to be read
on its own; where it appears the chart also carries a legend and a table view.
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go
import plotly.io as pio

# ── Categorical: identity. Fixed order, never cycled. ───────
CATEGORICAL_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a")  # blue, orange, aqua
CATEGORICAL_DARK = ("#3987e5", "#d95926", "#199e70")

# ── Sequential: magnitude, one hue light->dark. ─────────────
# The sunburst uses this rather than categorical colours: a category's spend
# is a *quantity*, and colouring the ring by identity would both imply the
# slices are unordered and blow past the three-slot cap.
SEQUENTIAL_BLUE = (
    "#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec",
    "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab",
)  # fmt: skip

# ── Status: state. Reserved — never reused as a series colour. ──
# Maps to CalloutLevel. Always shipped with a label, never colour alone:
# warning and serious are deliberately sub-3:1 on the light surface.
STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}

# Deliberately NOT a status role. An "info" callout is not an alarm, and the
# obvious choice — the categorical blue — would be a series colour
# impersonating a state, which is the one thing the reserved palette exists to
# prevent. Neutral ink instead: it reads as "noted", not "act".
INFORMATIONAL = "#898781"

_INK = {
    "light": {
        "surface": "#fcfcfb",
        "plane": "#f9f9f7",
        "primary": "#0b0b0b",
        "secondary": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "border": "rgba(11,11,11,0.10)",
    },
    "dark": {
        "surface": "#1a1a19",
        "plane": "#0d0d0d",
        "primary": "#ffffff",
        "secondary": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "border": "rgba(255,255,255,0.10)",
    },
}

FONT_STACK = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def ink(mode: str = "light") -> dict[str, str]:
    """Chrome colours for one mode."""
    return _INK[mode]


def rgba(hex_colour: str, alpha: float) -> str:
    """Translucent form of a palette colour.

    Plotly rejects 8-digit hex (``#2a78d666``) outright, so translucency has
    to be expressed as ``rgba()``. Used for Sankey links, where opaque ribbons
    would hide every crossing behind whichever was drawn last.
    """
    raw = hex_colour.lstrip("#")
    red, green, blue = (int(raw[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({red},{green},{blue},{alpha})"


def categorical(mode: str = "light") -> tuple[str, ...]:
    return CATEGORICAL_DARK if mode == "dark" else CATEGORICAL_LIGHT


def _template(mode: str) -> go.layout.Template:
    """Build the Plotly template for one mode.

    Grid and axes are deliberately recessive — a hairline grid and no plot
    border. The chart's job is the data; chrome that competes with it is
    chrome that has to be read past.
    """
    palette = ink(mode)
    axis = {
        "gridcolor": palette["grid"],
        "linecolor": palette["axis"],
        "zerolinecolor": palette["axis"],
        "tickfont": {"color": palette["muted"], "size": 12},
        "title": {"font": {"color": palette["secondary"], "size": 13}},
        "showline": False,
        "ticks": "",
    }
    return go.layout.Template(
        layout={
            "colorway": list(categorical(mode)),
            "paper_bgcolor": palette["surface"],
            "plot_bgcolor": palette["surface"],
            "font": {"family": FONT_STACK, "color": palette["secondary"], "size": 13},
            "title": {"font": {"color": palette["primary"], "size": 16}, "x": 0, "xanchor": "left"},
            "xaxis": {**axis, "showgrid": False},
            "yaxis": axis,
            "legend": {
                "orientation": "h",
                "yanchor": "bottom",
                "y": 1.02,
                "x": 0,
                "font": {"color": palette["secondary"]},
                "title": {"text": ""},
            },
            "margin": {"t": 48, "l": 8, "r": 8, "b": 8},
            "hoverlabel": {"font": {"family": FONT_STACK, "size": 13}},
            "colorscale": {"sequential": [[i / 9, c] for i, c in enumerate(SEQUENTIAL_BLUE)]},
        }
    )


TEMPLATE_LIGHT = "pf_light"
TEMPLATE_DARK = "pf_dark"


def register_templates() -> None:
    """Install both templates and make light the default.

    Called once at app import. Registering rather than passing a template to
    every figure means a page that forgets still gets the validated colours
    instead of Plotly's defaults.
    """
    pio.templates[TEMPLATE_LIGHT] = _template("light")
    pio.templates[TEMPLATE_DARK] = _template("dark")
    pio.templates.default = TEMPLATE_LIGHT


def figure_layout(mode: str = "light", **overrides: Any) -> dict[str, Any]:
    """Per-figure layout: the template plus whatever this chart needs."""
    return {"template": TEMPLATE_DARK if mode == "dark" else TEMPLATE_LIGHT, **overrides}
