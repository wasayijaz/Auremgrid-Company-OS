"""Zero-install launcher for a checked-out Auremgrid repository."""
from __future__ import annotations

import sys
from pathlib import Path


def _check_python() -> None:
    if sys.version_info < (3, 12):
        raise SystemExit("Auremgrid requires Python 3.12 or newer")


def main(argv: list[str] | None = None) -> int:
    _check_python()
    repo = Path(__file__).resolve().parents[1]
    source = repo / "src"
    cli = source / "auremgrid" / "cli.py"
    if not cli.is_file():
        raise SystemExit(f"Auremgrid source was not found: {cli}")
    sys.path.insert(0, str(source))
    from auremgrid.cli import main as cli_main

    return int(cli_main(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
