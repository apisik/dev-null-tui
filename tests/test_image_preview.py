import random

from PIL import Image

from dev_null_tty.image_preview import render_image_preview


def test_render_image_preview_emits_kitty_placeholder_grid(tmp_path) -> None:
    image_path = tmp_path / "preview.png"
    Image.new("RGB", (8, 4), (20, 40, 60)).save(image_path)

    preview = render_image_preview(image_path, 0x123456, 4, 2)

    assert b"\x1b_Ga=d,d=i,i=1193046,q=2;\x1b\\" in preview
    assert b"\x1b_Ga=t,f=100,t=d,i=1193046,q=2,m=0;" in preview
    assert b"\x1b_Ga=p,U=1,i=1193046,c=4,r=2,q=2;\x1b\\" in preview
    placeholder = chr(0x10EEEE)
    expected_placeholders = (
        b"\x1b[38;2;18;52;86m"
        + f"{placeholder}\u0305{placeholder * 3}\n{placeholder}\u030d{placeholder * 3}".encode(
            "utf-8"
        )
        + b"\x1b[39m"
    )
    assert expected_placeholders in preview


def test_render_image_preview_chunks_large_png_payload(tmp_path) -> None:
    image_path = tmp_path / "noise.png"
    pixels = random.Random(0).randbytes(48 * 48 * 3)
    Image.frombytes("RGB", (48, 48), pixels).save(image_path)

    preview = render_image_preview(image_path, 42, 20, 10)

    assert b"a=t,f=100,t=d,i=42,q=2,m=1;" in preview
    assert b"\x1b_Gm=1;" in preview
    assert b"\x1b_Gm=0;" in preview
