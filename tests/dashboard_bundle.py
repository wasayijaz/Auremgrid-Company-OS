from __future__ import annotations

from pathlib import Path


def read_dashboard_bundle(root: Path) -> str:
    dashboard = root.joinpath("src", "auremgrid", "api", "dashboard")
    assets = [
        dashboard.joinpath("index.html"),
        *sorted(dashboard.joinpath("css").glob("*.css")),
        *sorted(dashboard.joinpath("js").glob("*.js")),
        dashboard.joinpath("dashboard.css"),
        dashboard.joinpath("dashboard.js"),
    ]
    return "\n".join(path.read_text(encoding="utf-8") for path in assets)
