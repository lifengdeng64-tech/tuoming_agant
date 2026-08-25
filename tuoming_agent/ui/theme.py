from __future__ import annotations

ACCENT = "#0071E3"
INK = "#1D1D1F"
MUTED = "#86868B"
SUCCESS = "#248A3D"
WARNING = "#FF9F0A"
ERROR = "#D70015"
SERIES_COLORS = (ACCENT, "#5AC8FA", "#34C759", WARNING)

SYSTEM_FONT_STACK = (
    '-apple-system, BlinkMacSystemFont, "SF Pro Display", "PingFang SC", '
    '"Microsoft YaHei", sans-serif'
)

PLOTLY_CONFIG = {
    "displaylogo": False,
    "responsive": True,
    "scrollZoom": False,
    "toImageButtonOptions": {
        "format": "png",
        "filename": "tuoming-dashboard",
        "scale": 2,
    },
}

PLOTLY_LAYOUT = {
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "font": {"family": SYSTEM_FONT_STACK, "color": INK, "size": 12},
    "colorway": list(SERIES_COLORS),
    "margin": {"l": 16, "r": 16, "t": 54, "b": 16},
    "hoverlabel": {
        "bgcolor": "rgba(255,255,255,0.96)",
        "bordercolor": "#E5E5EA",
        "font": {"family": SYSTEM_FONT_STACK, "color": INK},
    },
    "xaxis": {"gridcolor": "#ECECF0", "zeroline": False, "title": None},
    "yaxis": {"gridcolor": "#ECECF0", "zeroline": False, "title": None},
}
