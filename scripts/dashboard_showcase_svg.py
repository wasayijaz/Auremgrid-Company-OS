from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "docs" / "assets" / "dashboard-showcase.svg"


def main() -> None:
    text = SVG.read_text(encoding="utf-8")
    required = ("SAMPLE DATA", "sample.invalid", "Ledger healthy", "Not connected")
    missing = [marker for marker in required if marker not in text]
    if missing:
        raise SystemExit(f"dashboard showcase SVG missing markers: {', '.join(missing)}")
    print(f"verified {SVG.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
