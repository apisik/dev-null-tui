"""Render local image previews with the Kitty graphics protocol."""

from __future__ import annotations

import argparse
import base64
from collections.abc import Sequence
from io import BytesIO
import os
from pathlib import Path
import sys

from PIL import Image, ImageOps

_PLACEHOLDER = chr(0x10EEEE)
_ROW_DIACRITICS = tuple(
    chr(codepoint)
    for codepoint in (
        0x0305, 0x030D, 0x030E, 0x0310, 0x0312, 0x033D, 0x033E, 0x033F,
        0x0346, 0x034A, 0x034B, 0x034C, 0x0350, 0x0351, 0x0352, 0x0357,
        0x035B, 0x0363, 0x0364, 0x0365, 0x0366, 0x0367, 0x0368, 0x0369,
        0x036A, 0x036B, 0x036C, 0x036D, 0x036E, 0x036F, 0x0483, 0x0484,
        0x0485, 0x0486, 0x0487, 0x0592, 0x0593, 0x0594, 0x0595, 0x0597,
        0x0598, 0x0599, 0x059C, 0x059D, 0x059E, 0x059F, 0x05A0, 0x05A1,
        0x05A8, 0x05A9, 0x05AB, 0x05AC, 0x05AF, 0x05C4, 0x0610, 0x0611,
        0x0612, 0x0613, 0x0614, 0x0615, 0x0616, 0x0617, 0x0657,
    )
)
_KITTY_ESCAPE = b"\x1b_G"
_KITTY_ESCAPE_END = b"\x1b\\"
_MAX_IMAGE_DIMENSIONS = (1280, 960)
_MAX_TRANSMISSION_CHUNK = 4096


def render_image_preview(
    image_path: Path,
    image_id: int,
    columns: int,
    rows: int,
) -> bytes:
    if not 0 < image_id <= 0xFFFFFF:
        raise ValueError("The Kitty image ID must fit in 24 bits.")
    if columns <= 0 or rows <= 0:
        raise ValueError("The fzf preview area must have positive dimensions.")

    rows = min(rows, len(_ROW_DIACRITICS))
    max_width = min(columns * 12, _MAX_IMAGE_DIMENSIONS[0])
    max_height = min(rows * 24, _MAX_IMAGE_DIMENSIONS[1])
    with Image.open(image_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
    image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    encoded_image = BytesIO()
    image.save(encoded_image, format="PNG", optimize=True)
    payload = base64.b64encode(encoded_image.getvalue())

    output = bytearray(
        _KITTY_ESCAPE
        + f"a=d,d=i,i={image_id},q=2;".encode("ascii")
        + _KITTY_ESCAPE_END
    )
    chunks = tuple(
        payload[offset : offset + _MAX_TRANSMISSION_CHUNK]
        for offset in range(0, len(payload), _MAX_TRANSMISSION_CHUNK)
    )
    for index, chunk in enumerate(chunks):
        more = int(index < len(chunks) - 1)
        parameters = (
            f"a=t,f=100,t=d,i={image_id},q=2,m={more};"
            if index == 0
            else f"m={more};"
        )
        output.extend(_KITTY_ESCAPE + parameters.encode("ascii") + chunk + _KITTY_ESCAPE_END)

    output.extend(
        _KITTY_ESCAPE
        + f"a=p,U=1,i={image_id},c={columns},r={rows},q=2;".encode("ascii")
        + _KITTY_ESCAPE_END
    )
    red = (image_id >> 16) & 0xFF
    green = (image_id >> 8) & 0xFF
    blue = image_id & 0xFF
    output.extend(f"\x1b[38;2;{red};{green};{blue}m".encode("ascii"))
    for row in range(rows):
        output.extend(_PLACEHOLDER.encode("utf-8"))
        output.extend(_ROW_DIACRITICS[row].encode("utf-8"))
        output.extend(_PLACEHOLDER.encode("utf-8") * (columns - 1))
        if row < rows - 1:
            output.extend(b"\n")
    output.extend(b"\x1b[39m")
    return bytes(output)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-id", type=int, required=True)
    parser.add_argument("image", type=Path)
    arguments = parser.parse_args(argv)

    try:
        columns = int(os.environ["FZF_PREVIEW_COLUMNS"])
        rows = int(os.environ["FZF_PREVIEW_LINES"])
        preview = render_image_preview(arguments.image, arguments.image_id, columns, rows)
    except (Image.DecompressionBombError, KeyError, OSError, ValueError) as error:
        print(f"Could not render image preview: {error}", file=sys.stderr)
        return 1

    sys.stdout.buffer.write(preview)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
