from __future__ import annotations

import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMAGE = ROOT / "docs" / "assets" / "dashboard-realistic-agency.jpg"


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("dashboard showcase must be a JPEG")
    index = 2
    while index < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        length = struct.unpack(">H", data[index:index + 2])[0]
        if marker in {0xC0, 0xC1, 0xC2}:
            height, width = struct.unpack(">HH", data[index + 3:index + 7])
            return width, height
        index += length
    raise ValueError("dashboard showcase has no JPEG dimensions")


def main() -> None:
    data = IMAGE.read_bytes()
    width, height = _jpeg_dimensions(data)
    if len(data) < 10_000 or width < 1_000 or height < 600:
        raise SystemExit("dashboard showcase image is unexpectedly small")
    print(f"verified {IMAGE.relative_to(ROOT)} ({width}x{height})")


if __name__ == "__main__":
    main()
