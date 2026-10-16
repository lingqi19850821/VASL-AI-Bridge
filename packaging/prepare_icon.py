"""Rebuild the application ICO from the two selected, local PNG assets."""

from __future__ import annotations

import io
from pathlib import Path
import struct

from PIL import Image


ASSETS = Path(__file__).resolve().parent.parent / "vasl_ai_bridge" / "assets"
SMALL = ASSETS / "app-icon-32.png"
LARGE = ASSETS / "app-icon-large.png"
OUTPUT = ASSETS / "app.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def prepare_icon() -> None:
    with Image.open(SMALL) as image:
        if image.size != (32, 32) or image.mode != "RGBA":
            raise ValueError("The selected small icon must be a 32x32 RGBA PNG.")
        small = image.copy()
    with Image.open(LARGE) as image:
        large = image.convert("RGBA")
    frames = []
    for size in SIZES:
        if size == 32:
            # Preserve the selected PNG bytes, including its exact pixel grid.
            frames.append(SMALL.read_bytes())
            continue
        if size < 64:
            frame = small.resize((size, size), Image.Resampling.NEAREST)
        else:
            scale = min(size / large.width, size / large.height)
            dimensions = (round(large.width * scale), round(large.height * scale))
            scaled = large.resize(dimensions, Image.Resampling.LANCZOS)
            frame = Image.new("RGBA", (size, size))
            frame.alpha_composite(scaled, ((size - scaled.width) // 2,
                                          (size - scaled.height) // 2))
        stream = io.BytesIO()
        frame.save(stream, format="PNG")
        frames.append(stream.getvalue())
    offset = 6 + 16 * len(SIZES)
    entries = []
    for size, data in zip(SIZES, frames, strict=True):
        dimension = 0 if size == 256 else size
        entries.append(struct.pack("<BBBBHHII", dimension, dimension, 0, 0,
                                   1, 32, len(data), offset))
        offset += len(data)
    OUTPUT.write_bytes(struct.pack("<HHH", 0, 1, len(SIZES))
                       + b"".join(entries) + b"".join(frames))
    print("Prepared app.ico: " + ", ".join(f"{size}px" for size in SIZES))


if __name__ == "__main__":
    prepare_icon()
