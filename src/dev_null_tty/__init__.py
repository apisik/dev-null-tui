"""A Textual client for /dev/null."""

import argparse
import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from io import BytesIO
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import webbrowser
from typing import Literal

from PIL import Image as PILImage, ImageOps
from rich.cells import cell_len
from rich.console import Console
from rich.markup import escape
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.events import Click, Resize
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    ContentSwitcher,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TextArea,
)

from .api import (
    ApiError,
    ApiCommit,
    ApiCommitContext,
    ApiCommitImage,
    ApiCommitPreview,
    ApiNotification,
    ApiUser,
    DeviceAuthorization,
    LogName,
    SignedApiClient,
    authorization_code_from_callback,
    create_device_authorization,
    existing_device_identity,
    remove_device_identity,
)
from .desktop import callback_handoff_path, register_uri_handler
from textual_image._terminal import get_cell_size
from textual_image.widget import Image as TerminalImage

ComposerMode = Literal["new", "reply", "quote"]
_URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_IMAGE_PREVIEW_ID = secrets.randbelow(0xFFFFFF) + 1


def _pick_images_with_fzf(
    max_images: int,
    *,
    candidates: tuple[Path, ...] | None = None,
    prompt: str = "Images> ",
) -> tuple[Path, ...]:
    if max_images <= 0 or candidates == ():
        return ()
    fzf = shutil.which("fzf")
    if fzf is None:
        raise ApiError("Install fzf to search for image files interactively.")
    if candidates is None:
        find = shutil.which("find")
        if find is None:
            raise ApiError("The find command is required to search for image files.")
        home = Path.home()
        if not home.is_dir():
            raise ApiError(f"Could not search for images: home directory {home} is unavailable.")
        find_arguments = [
            find,
            os.fspath(home),
            "-type",
            "f",
            "(",
            "-iname",
            "*.jpg",
            "-o",
            "-iname",
            "*.jpeg",
            "-o",
            "-iname",
            "*.png",
            "-o",
            "-iname",
            "*.webp",
            ")",
            "-print0",
        ]
        candidate_input = None
    else:
        find_arguments = None
        candidate_input = b"\0".join(os.fsencode(path) for path in candidates) + b"\0"
    fzf_arguments = [
        fzf,
        "--read0",
        "--print0",
        f"--multi={min(max_images, len(candidates)) if candidates is not None else max_images}",
        f"--prompt={prompt}",
        "--layout=reverse",
        "--border",
    ]
    term_program = os.environ.get("TERM_PROGRAM", "").casefold()
    supports_kitty_graphics = bool(
        os.environ.get("KITTY_WINDOW_ID")
        or os.environ.get("GHOSTTY_RESOURCES_DIR")
        or os.environ.get("TERM", "").casefold() in {"xterm-kitty", "xterm-ghostty"}
        or term_program in {"kitty", "ghostty"}
    )
    if supports_kitty_graphics:
        fzf_arguments.extend(
            (
                "--preview",
                f"{shlex.quote(sys.executable)} -m dev_null_tty.image_preview "
                f"--image-id {_IMAGE_PREVIEW_ID} {{}}",
                "--preview-window=right,50%,border-left",
            )
        )
    else:
        fzf_arguments.append("--header=Run in Kitty or Ghostty to preview images")

    finder: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryFile() as finder_errors:
            if find_arguments is not None:
                finder = subprocess.Popen(
                    find_arguments,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=finder_errors,
                )
                if finder.stdout is None:
                    raise ApiError("Could not open the image search output.")
                try:
                    selection = subprocess.run(
                        fzf_arguments,
                        stdin=finder.stdout,
                        check=False,
                        capture_output=True,
                    )
                finally:
                    finder.stdout.close()
                finder_returncode = finder.wait()
                finder_errors.seek(0)
                finder_error = finder_errors.read().decode("utf-8", errors="replace").strip()
            else:
                selection = subprocess.run(
                    fzf_arguments,
                    input=candidate_input,
                    check=False,
                    capture_output=True,
                )
                finder_returncode = 0
                finder_error = ""
    except OSError as error:
        if finder is not None and finder.poll() is None:
            finder.terminate()
            finder.wait()
        raise ApiError(f"Could not start image search: {error}") from error

    if selection.returncode == 130:
        return ()
    if finder_returncode not in {0, -signal.SIGPIPE}:
        detail = f" {finder_error}" if finder_error else ""
        raise ApiError(f"Could not search for images.{detail}")
    if selection.returncode == 1:
        return ()
    if selection.returncode != 0:
        detail = selection.stderr.decode("utf-8", errors="replace").strip()
        message = "fzf could not select image files."
        if detail:
            message = f"{message} {detail}"
        raise ApiError(message)

    return tuple(
        Path(os.fsdecode(path))
        for path in selection.stdout.split(b"\0")
        if path
    )


def _linkify_urls(content: str) -> Text:
    rendered = Text()
    position = 0
    for match in _URL_PATTERN.finditer(content):
        url = match.group().rstrip(".,!?;:")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        if not url:
            continue
        link_end = match.start() + len(url)
        rendered.append(content[position : match.start()])
        rendered.append(url, style=Style(link=url, color="#8be9fd", underline=True))
        position = link_end
    rendered.append(content[position:])
    return rendered


class LinkedText(Static):
    def __init__(
        self,
        content: str | Text,
        *,
        width_content_factory: Callable[[int], Text] | None = None,
        **kwargs,
    ) -> None:
        self.width_content_factory = width_content_factory
        super().__init__(content, **kwargs)

    def on_resize(self, event: Resize) -> None:
        content_width = self.content_region.width
        if self.width_content_factory is not None and content_width > 0:
            self.update(self.width_content_factory(content_width))

    async def on_click(self, event: Click) -> None:
        url = event.style.link
        if event.button != 1 or not url or not url.startswith(("http://", "https://")):
            return
        event.stop()
        try:
            opened = await asyncio.to_thread(webbrowser.open, url)
        except (OSError, webbrowser.Error) as error:
            self.app.notify(f"Could not open link: {error}", severity="error")
            return
        if not opened:
            self.app.notify("Could not open link in a browser.", severity="warning")


def _decode_commit_image(contents: bytes) -> PILImage.Image:
    with PILImage.open(BytesIO(contents)) as source:
        if source.width * source.height > 40_000_000:
            raise ValueError("The image dimensions are too large to display.")
        image = source.convert("RGB")
    image.thumbnail((1920, 1440), PILImage.Resampling.LANCZOS)
    return image


def _image_frame_pixel_size(
    frame_size: tuple[int, int],
    cell_size: tuple[int, int],
) -> tuple[int, int]:
    return max(1, frame_size[0] * cell_size[0]), max(1, frame_size[1] * cell_size[1])


def _image_fit_scale(
    image_size: tuple[int, int],
    frame_size: tuple[int, int],
    cell_size: tuple[int, int],
) -> float:
    frame_width, frame_height = _image_frame_pixel_size(frame_size, cell_size)
    return min(frame_width / image_size[0], frame_height / image_size[1])


def _image_crop_size(
    image_size: tuple[int, int],
    zoom: float,
    frame_size: tuple[int, int],
    cell_size: tuple[int, int],
) -> tuple[int, int]:
    width, height = image_size
    if zoom <= 1:
        return width, height
    frame_width, frame_height = _image_frame_pixel_size(frame_size, cell_size)
    scale = _image_fit_scale(image_size, frame_size, cell_size) * zoom
    return (
        min(width, max(1, round(frame_width / scale))),
        min(height, max(1, round(frame_height / scale))),
    )


def _image_pan_bounds(
    image_size: tuple[int, int],
    zoom: float,
    frame_size: tuple[int, int],
    cell_size: tuple[int, int],
) -> tuple[float, float, float, float]:
    width, height = image_size
    crop_width, crop_height = _image_crop_size(
        image_size, zoom, frame_size, cell_size
    )
    buffer_x = max(1, round(crop_width * 0.1))
    buffer_y = max(1, round(crop_height * 0.1))
    min_x = (crop_width / 2 - buffer_x) / width
    min_y = (crop_height / 2 - buffer_y) / height
    return min_x, 1 - min_x, min_y, 1 - min_y


def _image_crop_box(
    image_size: tuple[int, int],
    zoom: float,
    pan_x: float,
    pan_y: float,
    frame_size: tuple[int, int],
    cell_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    width, height = image_size
    crop_width, crop_height = _image_crop_size(
        image_size, zoom, frame_size, cell_size
    )
    left = round(pan_x * width - crop_width / 2)
    top = round(pan_y * height - crop_height / 2)
    return left, top, left + crop_width, top + crop_height


def _render_image_minimap(
    image: PILImage.Image,
    zoom: float,
    pan_x: float,
    pan_y: float,
    frame_size: tuple[int, int],
    cell_size: tuple[int, int],
) -> Text:
    columns, rows = 15, 7
    pixel_rows = rows * 2
    thumbnail = ImageOps.contain(
        image, (columns, pixel_rows), PILImage.Resampling.LANCZOS
    )
    map_width, map_height = thumbnail.size
    offset_x = (columns - map_width) // 2
    offset_y = (pixel_rows - map_height) // 2
    min_x, max_x, min_y, max_y = _image_pan_bounds(
        image.size,
        zoom,
        frame_size,
        cell_size,
    )
    dot_x = offset_x + round(min(max(pan_x, 0), 1) * (map_width - 1))
    dot_y = (
        offset_y + round(min(max(pan_y, 0), 1) * (map_height - 1))
    ) // 2
    box_x0 = offset_x
    box_x1 = offset_x + map_width - 1
    box_y0 = offset_y // 2
    box_y1 = (offset_y + map_height - 1) // 2
    edge_left = pan_x <= min_x
    edge_right = pan_x >= max_x
    edge_top = pan_y <= min_y
    edge_bottom = pan_y >= max_y
    minimap = Text()
    for y in range(rows):
        for x in range(columns):
            pixel_x = x - offset_x
            top_y = y * 2 - offset_y
            bottom_y = top_y + 1
            if 0 <= pixel_x < map_width and 0 <= top_y < map_height:
                top_color = thumbnail.getpixel((pixel_x, top_y))
            else:
                top_color = (16, 20, 24)
            if 0 <= pixel_x < map_width and 0 <= bottom_y < map_height:
                bottom_color = thumbnail.getpixel((pixel_x, bottom_y))
            else:
                bottom_color = (16, 20, 24)
            image_background = f"#{bottom_color[0]:02x}{bottom_color[1]:02x}{bottom_color[2]:02x}"
            if x == dot_x and y == dot_y:
                character = "·"
                color = "#ffd166"
            elif x == box_x0 and y == box_y0:
                character = "┌"
                color = "#ff5c5c" if edge_left or edge_top else "#687781"
            elif x == box_x1 and y == box_y0:
                character = "┐"
                color = "#ff5c5c" if edge_right or edge_top else "#687781"
            elif x == box_x0 and y == box_y1:
                character = "└"
                color = "#ff5c5c" if edge_left or edge_bottom else "#687781"
            elif x == box_x1 and y == box_y1:
                character = "┘"
                color = "#ff5c5c" if edge_right or edge_bottom else "#687781"
            elif y == box_y0 and box_x0 <= x <= box_x1:
                character = "─"
                color = "#ff5c5c" if edge_top else "#687781"
            elif y == box_y1 and box_x0 <= x <= box_x1:
                character = "─"
                color = "#ff5c5c" if edge_bottom else "#687781"
            elif x == box_x0 and box_y0 <= y <= box_y1:
                character = "│"
                color = "#ff5c5c" if edge_left else "#687781"
            elif x == box_x1 and box_y0 <= y <= box_y1:
                character = "│"
                color = "#ff5c5c" if edge_right else "#687781"
            else:
                character = "▀"
                top_color_hex = f"#{top_color[0]:02x}{top_color[1]:02x}{top_color[2]:02x}"
                color = top_color_hex
            style = Style(color=color, bgcolor=image_background)
            minimap.append(character, style=style)
        if y < rows - 1:
            minimap.append("\n")
    return minimap


class CommitImagePreview(Vertical):
    def __init__(
        self,
        image: ApiCommitImage | None = None,
        client: SignedApiClient | None = None,
        *,
        classes: str = "commit-image-preview",
        on_loaded: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(classes=classes)
        self.image_info = image
        self.client = client
        self.local_image_path: Path | None = None
        self._loaded = False
        self._loading = False
        self._generation = 0
        self._source_image: PILImage.Image | None = None
        self._zoom = 1.0
        self._pan_x = 0.5
        self._pan_y = 0.5
        self._on_loaded = on_loaded
        self.display = image is not None

    def compose(self) -> ComposeResult:
        yield TerminalImage(classes="terminal-image")
        yield Static("", classes="commit-image-status")

    def set_image(
        self,
        image: ApiCommitImage | None,
        client: SignedApiClient | None,
    ) -> None:
        if (
            self.image_info == image
            and self.client is client
            and self.local_image_path is None
        ):
            return
        self.image_info = image
        self.client = client
        self.local_image_path = None
        self._reset_source()

    def set_local_image(self, image_path: Path) -> None:
        if self.local_image_path == image_path and self.image_info is None:
            return
        self.image_info = None
        self.client = None
        self.local_image_path = image_path
        self._reset_source()

    def _reset_source(self) -> None:
        self._loaded = False
        self._loading = False
        self._generation += 1
        self._source_image = None
        has_image = self.image_info is not None or self.local_image_path is not None
        self.display = has_image
        if self.is_mounted:
            self.query_one(TerminalImage).image = None
            status = self.query_one(".commit-image-status", Static)
            status.update("Loading image..." if has_image else "")
            status.display = has_image

    def set_transform(self, zoom: float, pan_x: float, pan_y: float) -> None:
        self._zoom = zoom
        self._pan_x = pan_x
        self._pan_y = pan_y
        self._render_transformed_image()

    def on_resize(self, event: Resize) -> None:
        self._render_transformed_image()

    @property
    def source_image(self) -> PILImage.Image | None:
        return self._source_image

    def load(self) -> None:
        if (
            not self.is_mounted
            or (
                self.local_image_path is None
                and (self.image_info is None or self.client is None)
            )
            or self._loaded
            or self._loading
        ):
            return
        self._loading = True
        self._generation += 1
        generation = self._generation
        status = self.query_one(".commit-image-status", Static)
        status.update("Loading image...")
        status.display = True
        self.run_worker(
            self._load_image(generation),
            group="commit-image",
            exclusive=True,
        )

    def unload(self) -> None:
        self._generation += 1
        self._loaded = False
        self._loading = False
        self._source_image = None
        if self.is_mounted:
            self.query_one(TerminalImage).image = None
            status = self.query_one(".commit-image-status", Static)
            status.update("")
            status.display = False

    async def _load_image(self, generation: int) -> None:
        image = self.image_info
        client = self.client
        image_path = self.local_image_path
        try:
            if image_path is not None:
                contents = await asyncio.to_thread(image_path.read_bytes)
            elif image is not None and client is not None:
                contents = await asyncio.to_thread(client.get_commit_image, image)
            else:
                return
            preview = await asyncio.to_thread(_decode_commit_image, contents)
        except (ApiError, OSError, ValueError, PILImage.DecompressionBombError) as error:
            if self.is_mounted and generation == self._generation:
                self.query_one(".commit-image-status", Static).update(
                    f"[dim]Image unavailable: {escape(str(error))}[/dim]"
                )
        else:
            if self.is_mounted and generation == self._generation:
                self._source_image = preview
                status = self.query_one(".commit-image-status", Static)
                status.display = False
                self._loaded = True
                self.set_timer(0.05, self._render_transformed_image)
                if self._on_loaded is not None:
                    self._on_loaded()
        finally:
            if generation == self._generation:
                self._loading = False

    def _render_transformed_image(self) -> None:
        source = self._source_image
        if not self.is_mounted or source is None:
            return
        terminal_image = self.query_one(TerminalImage)
        frame_size = (terminal_image.size.width, terminal_image.size.height)
        if min(frame_size) <= 1:
            return
        cell_size = get_cell_size()
        frame_pixels = _image_frame_pixel_size(
            frame_size,
            (cell_size.width, cell_size.height),
        )
        if self._zoom <= 1:
            visible = source
        else:
            visible = source.crop(
                _image_crop_box(
                    source.size,
                    self._zoom,
                    self._pan_x,
                    self._pan_y,
                    frame_size,
                    (cell_size.width, cell_size.height),
                )
            )
        fitted = ImageOps.contain(visible, frame_pixels, PILImage.Resampling.LANCZOS)
        rendered = PILImage.new("RGB", frame_pixels, "#101418")
        rendered.paste(
            fitted,
            ((frame_pixels[0] - fitted.width) // 2, (frame_pixels[1] - fitted.height) // 2),
        )
        terminal_image.image = rendered
        terminal_image.refresh(layout=True)


class CommitImageViewer(Vertical):
    MAX_ZOOM_STEP = 5

    def __init__(
        self,
        client: SignedApiClient | None,
        *,
        id: str,
    ) -> None:
        super().__init__(id=id)
        self.client = client
        self.images: tuple[ApiCommitImage, ...] = ()
        self.local_images: tuple[Path, ...] = ()
        self.index = 0
        self.active = False
        self.zoom_step = 0
        self.pan_x = 0.5
        self.pan_y = 0.5

    def compose(self) -> ComposeResult:
        yield Label("", id="thread-image-caption")
        with Horizontal(id="thread-image-content"):
            yield CommitImagePreview(
                classes="detail-image-preview",
                on_loaded=self._update_minimap,
            )
            with Vertical(id="thread-image-map"):
                yield Label("OVERVIEW", id="thread-image-minimap-title")
                yield Static("", id="thread-image-minimap")

    def set_images(self, images: tuple[ApiCommitImage, ...]) -> None:
        if images != self.images or self.local_images:
            self.index = 0
            self._reset_view()
        self.images = images
        self.local_images = ()
        self._show_current_image(load=self.active)

    def set_local_images(self, images: tuple[Path, ...]) -> None:
        if images != self.local_images or self.images:
            self.index = 0
            self._reset_view()
        self.images = ()
        self.local_images = images
        self._show_current_image(load=self.active)

    def next_image(self) -> None:
        if self.image_count and self.index < self.image_count - 1:
            self.index += 1
            self._reset_view()
            self._show_current_image(load=True)

    def previous_image(self) -> None:
        if self.image_count and self.index > 0:
            self.index -= 1
            self._reset_view()
            self._show_current_image(load=True)

    def zoom_in(self) -> None:
        if self.image_count and self.zoom_step < self.MAX_ZOOM_STEP:
            self.zoom_step += 1
            self._update_transform()

    def zoom_out(self) -> None:
        if self.image_count and self.zoom_step > 0:
            self.zoom_step -= 1
            if self.zoom_step == 0:
                self.pan_x = 0.5
                self.pan_y = 0.5
            self._update_transform()

    def pan(self, horizontal: int = 0, vertical: int = 0) -> None:
        if not self.image_count or self.zoom_step == 0:
            return
        distance = 0.2 / self.zoom_factor
        preview = self.query_one(CommitImagePreview)
        source_image = preview.source_image
        if source_image is None:
            return
        terminal_image = preview.query_one(TerminalImage)
        cell_size = get_cell_size()
        min_x, max_x, min_y, max_y = _image_pan_bounds(
            source_image.size,
            self.zoom_factor,
            (terminal_image.size.width, terminal_image.size.height),
            (cell_size.width, cell_size.height),
        )
        self.pan_x = min(max(self.pan_x + horizontal * distance, min_x), max_x)
        self.pan_y = min(max(self.pan_y + vertical * distance, min_y), max_y)
        self._update_transform()

    @property
    def zoom_factor(self) -> float:
        return 1.5**self.zoom_step

    @property
    def image_count(self) -> int:
        return len(self.images) + len(self.local_images)

    def load_current_image(self) -> None:
        if self.image_count:
            self.query_one(CommitImagePreview).load()

    def deactivate(self) -> None:
        self.active = False
        self.query_one(CommitImagePreview).unload()
        self._update_minimap()

    def activate(self) -> None:
        self.active = True
        self.load_current_image()

    def _reset_view(self) -> None:
        self.zoom_step = 0
        self.pan_x = 0.5
        self.pan_y = 0.5

    def _update_transform(self) -> None:
        if not self.is_mounted:
            return
        self.query_one(CommitImagePreview).set_transform(
            self.zoom_factor,
            self.pan_x,
            self.pan_y,
        )
        self._update_caption()
        self._update_minimap()

    def _update_minimap(self) -> None:
        if not self.is_mounted:
            return
        preview = self.query_one(CommitImagePreview)
        minimap_panel = self.query_one("#thread-image-map", Vertical)
        source_image = preview.source_image
        show_minimap = self.active and self.zoom_step > 0 and source_image is not None
        minimap_panel.display = show_minimap
        if show_minimap and source_image is not None:
            terminal_image = preview.query_one(TerminalImage)
            cell_size = get_cell_size()
            self.query_one("#thread-image-minimap", Static).update(
                _render_image_minimap(
                    source_image,
                    self.zoom_factor,
                    self.pan_x,
                    self.pan_y,
                    (terminal_image.size.width, terminal_image.size.height),
                    (cell_size.width, cell_size.height),
                )
            )

    def _update_caption(self) -> None:
        if not self.is_mounted or not self.image_count:
            return
        description = ""
        if self.images:
            image_description = self.images[self.index].description
            description = f" · {escape(image_description)}" if image_description else ""
        self.query_one("#thread-image-caption", Label).update(
            f"IMAGE {self.index + 1} / {self.image_count} · "
            f"ZOOM {self.zoom_factor:.1f}x{description}"
        )

    def _show_current_image(self, *, load: bool = False) -> None:
        if not self.is_mounted:
            return
        preview = self.query_one(CommitImagePreview)
        if not self.image_count:
            self.query_one("#thread-image-caption", Label).update("No attached images.")
            preview.set_image(None, self.client)
            return
        self._update_caption()
        if self.local_images:
            preview.set_local_image(self.local_images[self.index])
        else:
            preview.set_image(self.images[self.index], self.client)
        preview.set_transform(self.zoom_factor, self.pan_x, self.pan_y)
        self._update_minimap()
        if load:
            preview.load()


class ComposerImageViewer(CommitImageViewer):
    BINDINGS = [
        Binding("n", "next_image", "Next image", show=False, priority=True),
        Binding("ctrl+n", "previous_image", "Previous image", show=False, priority=True),
        Binding("-", "zoom_out", "Zoom out", show=False, priority=True),
        Binding("=", "zoom_in", "Zoom in", show=False, priority=True),
        Binding("h", "pan_left", "Pan left", show=False, priority=True),
        Binding("j", "pan_down", "Pan down", show=False, priority=True),
        Binding("k", "pan_up", "Pan up", show=False, priority=True),
        Binding("l", "pan_right", "Pan right", show=False, priority=True),
    ]
    can_focus = True

    def __init__(self) -> None:
        super().__init__(None, id="composer-image-view")

    def action_next_image(self) -> None:
        self.next_image()

    def action_previous_image(self) -> None:
        self.previous_image()

    def action_zoom_in(self) -> None:
        self.zoom_in()

    def action_zoom_out(self) -> None:
        self.zoom_out()

    def action_pan_left(self) -> None:
        self.pan(horizontal=-1)

    def action_pan_right(self) -> None:
        self.pan(horizontal=1)

    def action_pan_up(self) -> None:
        self.pan(vertical=-1)

    def action_pan_down(self) -> None:
        self.pan(vertical=1)


class CommitCard(ListItem):
    """A single commit in a log."""

    def __init__(self, commit: ApiCommit, *, thread_view: bool = False) -> None:
        self.commit = commit
        self.thread_view = thread_view
        children = [
            LinkedText(
                _commit_display(
                    commit,
                    600,
                    show_counts=True,
                    thread_view=thread_view,
                ),
                classes="commit-card",
                width_content_factory=lambda width: _commit_display(
                    self.commit,
                    600,
                    show_counts=True,
                    width=width,
                    thread_view=self.thread_view,
                ),
            )
        ]
        super().__init__(*children)

    def update_commit(self, commit: ApiCommit) -> None:
        self.commit = commit
        text = self.query_one(LinkedText)
        text.update(
            _commit_display(
                commit,
                600,
                show_counts=True,
                width=max(1, text.content_region.width),
                thread_view=self.thread_view,
            )
        )


class NotificationCard(ListItem):
    def __init__(self, notification: ApiNotification) -> None:
        self.notification = notification
        super().__init__(
            LinkedText(
                _notification_display(notification),
                classes="notification-card",
                width_content_factory=lambda width: _notification_display(
                    self.notification,
                    width=width,
                ),
            )
        )

    def update_commit(self, commit: ApiCommit) -> None:
        self.notification = replace(self.notification, commit=commit)
        text = self.query_one(LinkedText)
        text.update(
            _notification_display(
                self.notification,
                width=max(1, text.content_region.width),
            )
        )


class JournalListView(ListView):
    def __init__(self, log_name: LogName) -> None:
        self.log_name = log_name
        super().__init__(id=f"{log_name}-list")

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self.is_mounted and self.is_vertical_scroll_end:
            self.app.call_after_refresh(
                self.app._on_journal_scroll_end,
                self.log_name,
            )


class RepliesListView(ListView):
    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self.is_mounted and self.is_vertical_scroll_end:
            self.app.call_after_refresh(self.app._load_more_commit_replies)


class NotificationsListView(ListView):
    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self.is_mounted and self.is_vertical_scroll_end:
            self.app.call_after_refresh(self.app._on_notifications_scroll_end)


class CommitComposerScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("ctrl+f", "find_images", "Attach images", show=False, priority=True),
        Binding("ctrl+i", "toggle_image_view", "Preview images", show=False, priority=True),
        Binding(
            "ctrl+r",
            "choose_images_to_remove",
            "Remove images",
            show=False,
            priority=True,
        ),
        Binding("ctrl+q", "cancel", "Close dialog", show=False, priority=True),
        Binding("escape", "cancel", "Close dialog", show=False, priority=True),
    ]

    def __init__(
        self,
        mode: ComposerMode,
        commit: ApiCommit | None = None,
    ) -> None:
        if mode != "new" and commit is None:
            raise ValueError(f"A {mode} composer needs a selected commit.")
        super().__init__()
        self.mode = mode
        self.commit = commit
        self.image_paths: list[Path] = []

    def compose(self) -> ComposeResult:
        if self.mode == "reply":
            title = f"Reply to @{self.commit.username}"
        elif self.mode == "quote":
            title = f"Quote @{self.commit.username}"
        else:
            title = "New commit"

        with Container(id="composer-dialog"):
            yield Label(title, id="composer-title")
            if self.commit is not None:
                yield LinkedText(
                    _commit_display(self.commit, 1200),
                    id="composer-context",
                    width_content_factory=lambda width: _commit_display(
                        self.commit,
                        1200,
                        width=width,
                    ),
                )
            with ContentSwitcher(initial="composer-form", id="composer-mode"):
                with Vertical(id="composer-form"):
                    yield TextArea(
                        placeholder="What would you like to share?",
                        id="commit-body",
                    )
                    if self.mode != "quote":
                        yield Label(
                            "Images: JPEG, PNG, or WebP; up to 3 files, 10 MB each.",
                            id="composer-images-label",
                        )
                        yield Static("No images attached.", id="composer-images")
                if self.mode != "quote":
                    yield ComposerImageViewer()
            yield Static("", id="composer-status")
            yield Static(
                (
                    "[bold cyan]Ctrl+F[/] attach · [bold cyan]Ctrl+R[/] remove · "
                    "[bold cyan]Ctrl+I[/] preview · [bold cyan]Ctrl+P[/] post · "
                    "[bold cyan]Esc[/] cancel"
                    if self.mode != "quote"
                    else "[bold cyan]Ctrl+P[/] post · [bold cyan]Esc[/] cancel"
                ),
                id="composer-help",
            )
            with Horizontal(id="composer-actions"):
                if self.mode != "quote":
                    yield Button("Find images", id="find-images")
                    yield Button(
                        "Remove images",
                        id="remove-image",
                        disabled=True,
                    )
                yield Button(
                    "Post",
                    variant="primary",
                    id="submit-commit",
                    disabled=True,
                )
                yield Button("Cancel", id="cancel-commit")

    def add_image(self, path: Path) -> str | None:
        if self.mode == "quote":
            return "Image attachments are not supported for quotes."
        if len(self.image_paths) >= 3:
            return "A commit can carry at most 3 images."
        if not path.is_file():
            return f"Image file does not exist: {path}"
        self.image_paths.append(path)
        names = ", ".join(escape(image.name) for image in self.image_paths)
        self.query_one("#composer-images", Static).update(f"Attached: {names}")
        self.query_one("#remove-image", Button).disabled = False
        self.query_one("#composer-image-view", ComposerImageViewer).set_local_images(
            tuple(self.image_paths)
        )
        return None

    def remove_images(self, paths: tuple[Path, ...]) -> None:
        selected = set(paths)
        self.image_paths[:] = [image for image in self.image_paths if image not in selected]
        names = ", ".join(escape(image.name) for image in self.image_paths)
        self.query_one("#composer-images", Static).update(
            f"Attached: {names}" if names else "No images attached."
        )
        self.query_one("#remove-image", Button).disabled = not self.image_paths
        viewer = self.query_one("#composer-image-view", ComposerImageViewer)
        viewer.set_local_images(tuple(self.image_paths))
        if not self.image_paths:
            self.query_one("#composer-mode", ContentSwitcher).current = "composer-form"
            self.query_one("#composer-dialog", Container).set_class(
                False,
                "image-view-active",
            )
            viewer.deactivate()
            self.query_one("#commit-body", TextArea).focus()
            self._show_composer_keybinds()

    def _show_composer_keybinds(self) -> None:
        if self.mode == "quote":
            self.query_one("#composer-help", Static).update(
                "[bold cyan]Ctrl+P[/] post · [bold cyan]Esc[/] cancel"
            )
        else:
            self.query_one("#composer-help", Static).update(
                "[bold cyan]Ctrl+F[/] attach · [bold cyan]Ctrl+R[/] remove · "
                "[bold cyan]Ctrl+I[/] preview · [bold cyan]Ctrl+P[/] post · "
                "[bold cyan]Esc[/] cancel"
            )

    def action_toggle_image_view(self) -> None:
        if self.mode == "quote" or not self.image_paths:
            self.query_one("#composer-status", Static).update(
                "[dim]Attach an image to preview it.[/dim]"
            )
            return
        mode = self.query_one("#composer-mode", ContentSwitcher)
        viewer = self.query_one("#composer-image-view", ComposerImageViewer)
        status = self.query_one("#composer-status", Static)
        if mode.current == "composer-image-view":
            viewer.deactivate()
            mode.current = "composer-form"
            self.query_one("#composer-dialog", Container).set_class(
                False,
                "image-view-active",
            )
            self.query_one("#commit-body", TextArea).focus()
            status.update("")
            self._show_composer_keybinds()
        else:
            viewer.activate()
            mode.current = "composer-image-view"
            self.query_one("#composer-dialog", Container).set_class(
                True,
                "image-view-active",
            )
            viewer.focus()
            self.query_one("#composer-help", Static).update(
                "[bold cyan]Ctrl+I[/] back · [bold cyan]n[/] next · "
                "[bold cyan]Ctrl+N[/] previous · [bold cyan]= / -[/] zoom · "
                "[bold cyan]h / j / k / l[/] pan · [bold cyan]Ctrl+P[/] post"
            )

    def on_mount(self) -> None:
        self.query_one("#commit-body", TextArea).focus()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "commit-body":
            self.query_one("#submit-commit", Button).disabled = not bool(
                event.text_area.text.strip()
            )

    async def action_cancel(self) -> None:
        await self.app.pop_screen()

    async def action_choose_images_to_remove(self) -> None:
        if self.mode == "quote" or not self.image_paths:
            return
        picker_button = self.query_one("#remove-image", Button)
        if picker_button.disabled:
            return
        picker_button.disabled = True
        status = self.query_one("#composer-status", Static)
        status.update("[dim]Opening image removal...[/dim]")
        try:
            with self.app.suspend():
                selected_paths = _pick_images_with_fzf(
                    len(self.image_paths),
                    candidates=tuple(self.image_paths),
                    prompt="Remove images> ",
                )
        except ApiError as error:
            status.update(f"[bold red]{escape(str(error))}[/bold red]")
        else:
            if selected_paths:
                self.remove_images(selected_paths)
            status.update("")
        finally:
            if picker_button.is_mounted:
                picker_button.disabled = not self.image_paths

    async def action_find_images(self) -> None:
        if self.mode == "quote":
            return
        remaining = 3 - len(self.image_paths)
        status = self.query_one("#composer-status", Static)
        if remaining == 0:
            status.update("[bold red]A commit can carry at most 3 images.[/bold red]")
            return

        picker_button = self.query_one("#find-images", Button)
        if picker_button.disabled:
            return
        picker_button.disabled = True
        status.update("[dim]Opening image search...[/dim]")
        try:
            with self.app.suspend():
                selected_paths = _pick_images_with_fzf(remaining)
        except ApiError as error:
            status.update(f"[bold red]{escape(str(error))}[/bold red]")
        else:
            if not selected_paths:
                status.update("")
                return
            for path in selected_paths:
                error = self.add_image(path)
                if error is not None:
                    status.update(f"[bold red]{escape(error)}[/bold red]")
                    return
            status.update("")
        finally:
            if picker_button.is_mounted:
                picker_button.disabled = False


class NotificationDetailScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("ctrl+q", "cancel", "Close notification", show=False, priority=True),
        Binding("escape", "cancel", "Close notification", show=False, priority=True),
    ]

    def __init__(self, notification: ApiNotification) -> None:
        super().__init__()
        self.notification = notification

    def compose(self) -> ComposeResult:
        with Container(id="notification-detail-dialog"):
            yield Label("NOTIFICATION", id="notification-detail-title")
            yield LinkedText(
                _notification_display(self.notification),
                id="notification-detail-content",
            )
            yield Static("Esc close", id="notification-detail-help")

    async def action_cancel(self) -> None:
        await self.app.pop_screen()


@dataclass
class CommitThreadLevel:
    commit: ApiCommit
    replies: list[ApiCommit] = field(default_factory=list)
    next_cursor: str | None = None
    replies_loaded: bool = False
    loading_replies: bool = False
    failed_cursor: str | None = None
    failed_message: str | None = None
    requested_cursors: set[str | None] = field(default_factory=set)
    selected_index: int | None = None
    generation: int = 0


class CommitDetailScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("j", "next_reply", "Next reply", show=False, priority=True),
        Binding("k", "previous_reply", "Previous reply", show=False, priority=True),
        Binding("ctrl+j", "scroll_history_down", show=False, priority=True),
        Binding("ctrl+k", "scroll_history_up", show=False, priority=True),
        Binding("alt+j", "scroll_current_commit_down", show=False, priority=True),
        Binding("alt+k", "scroll_current_commit_up", show=False, priority=True),
        Binding("ctrl+r", "reply_to_current", "Reply", show=False, priority=True),
        Binding("s", "toggle_star_current", "Toggle star", show=False, priority=True),
        Binding("i", "toggle_image_view", "Images", show=False, priority=True),
        Binding("n", "next_image", "Next image", show=False, priority=True),
        Binding("ctrl+n", "previous_image", "Previous image", show=False, priority=True),
        Binding("-", "zoom_out", "Zoom out", show=False, priority=True),
        Binding("=", "zoom_in", "Zoom in", show=False, priority=True),
        Binding("h", "pan_left", "Pan left", show=False, priority=True),
        Binding("l", "pan_right", "Pan right", show=False, priority=True),
        Binding("u", "parent_comment", "Parent comment", show=False, priority=True),
        Binding("q", "back", "Back", show=False, priority=True),
        Binding("escape", "back", "Back", show=False, priority=True),
        Binding("r", "retry_replies", "Retry replies", show=False, priority=True),
    ]

    def __init__(self, commit: ApiCommit, client: SignedApiClient) -> None:
        super().__init__()
        self.client = client
        self.levels = [CommitThreadLevel(commit)]
        self.ancestor_context: tuple[ApiCommitContext, ...] = ()
        self.parent_navigation_loading = False
        self.image_view_active = False

    def compose(self) -> ComposeResult:
        with Container(id="commit-detail-dialog"):
            yield Label("COMMIT THREAD", id="commit-detail-title")
            with ContentSwitcher(initial="thread-columns", id="thread-mode"):
                with Horizontal(id="thread-columns"):
                    with VerticalScroll(id="thread-history-pane"):
                        yield LinkedText(
                            self._thread_display(),
                            id="thread-context",
                            width_content_factory=lambda width: self._thread_display(width),
                        )
                    with Vertical(id="thread-replies-pane"):
                        yield Label("REPLYING TO", id="current-commit-title")
                        with VerticalScroll(id="current-commit-pane"):
                            yield LinkedText(
                                self._current_commit_display(),
                                id="current-commit",
                                width_content_factory=lambda width: self._current_commit_display(
                                    width
                                ),
                            )
                        yield Label("", id="replies-title")
                        yield Static("Loading replies...", id="replies-status")
                        yield RepliesListView(id="commit-replies")
                yield CommitImageViewer(self.client, id="thread-image-view")
            yield Static(
                "Ctrl+j/k scroll ancestors · Alt+j/k scroll replying-to · j/k select reply · Enter follow · i images · Ctrl+R reply · u parent · q/Esc back · r retry",
                id="detail-help",
            )

    def on_mount(self) -> None:
        self._show_current_level()
        self.load_more_replies()
        self.app.run_worker(
            self._load_commit_context(self.levels[0].commit.id),
            group=f"commit-context-{self.levels[0].commit.id}",
            exclusive=True,
        )

    @property
    def current_level(self) -> CommitThreadLevel:
        return self.levels[-1]

    def _thread_display(self, width: int | None = None) -> Text:
        rendered = Text()
        chain: list[ApiCommit | ApiCommitContext] = [
            *self.ancestor_context,
            *(level.commit for level in self.levels[:-1]),
        ]
        if not chain:
            return Text("No earlier context.", style="dim")
        for index, commit in enumerate(chain):
            if index:
                rendered.append("\n\n")
            rendered.append(f"#{index + 1} ", style="bold cyan")
            if isinstance(commit, ApiCommitContext):
                rendered.append(_commit_context_display(commit))
            else:
                rendered.append(
                    _commit_display(
                        commit,
                        max(len(commit.body), 1),
                        show_counts=True,
                        width=width,
                        thread_view=True,
                    )
                )
        return rendered

    def _current_commit_display(self, width: int | None = None) -> Text:
        commit = self.current_level.commit
        return _commit_display(
            commit,
            max(len(commit.body), 1),
            show_counts=True,
            width=width,
            thread_view=True,
            include_quote_text=True,
        )

    def _scroll_current_commit(self, direction: int) -> None:
        current_pane = self.query_one("#current-commit-pane", VerticalScroll)
        current_pane.scroll_to(
            y=current_pane.scroll_y + direction * 3,
            animate=False,
            force=True,
        )

    def _scroll_history(self, direction: int) -> None:
        history = self.query_one("#thread-history-pane", VerticalScroll)
        history.scroll_to(
            y=history.scroll_y + direction * 3,
            animate=False,
            force=True,
        )

    def action_scroll_history_down(self) -> None:
        self._scroll_history(1)

    def action_scroll_history_up(self) -> None:
        self._scroll_history(-1)

    def action_scroll_current_commit_down(self) -> None:
        self._scroll_current_commit(1)

    def action_scroll_current_commit_up(self) -> None:
        self._scroll_current_commit(-1)

    async def _load_commit_context(self, commit_id: str) -> None:
        try:
            detail = await asyncio.to_thread(self.client.get_commit_detail, commit_id)
        except (ApiError, OSError) as error:
            if self.is_mounted:
                self.app.notify(
                    f"Could not load the full comment chain: {error}",
                    severity="warning",
                )
        else:
            if not self.is_mounted or self.levels[0].commit.id != commit_id:
                return
            self.levels[0].commit = detail.commit
            self.ancestor_context = detail.ancestors
            self._show_current_level()

    def _show_current_level(self) -> None:
        level = self.current_level
        thread_context_text = self.query_one("#thread-context", LinkedText)
        thread_context_text.update(
            self._thread_display(max(1, thread_context_text.content_region.width))
        )
        current_commit_text = self.query_one("#current-commit", LinkedText)
        current_commit_text.update(
            self._current_commit_display(
                max(1, current_commit_text.content_region.width)
            )
        )
        self.query_one("#replies-title", Label).update(
            f"REPLIES / @{level.commit.username} · {level.commit.replies_count}"
        )
        reply_list = self.query_one("#commit-replies", RepliesListView)
        reply_list.clear()
        reply_list.extend(
            CommitCard(reply, thread_view=True) for reply in level.replies
        )
        self.query_one("#thread-image-view", CommitImageViewer).set_images(level.commit.images)
        self.query_one("#detail-help", Static).update(
            self._image_view_help_text()
            if self.image_view_active
            else self._detail_help_text()
        )
        if level.selected_index is not None and level.selected_index < len(level.replies):
            reply_list.index = level.selected_index
        else:
            reply_list.index = None
        self._update_reply_status(level)
        reply_list.focus()

    def _update_reply_status(self, level: CommitThreadLevel) -> None:
        status = self.query_one("#replies-status", Static)
        if level.loading_replies:
            status.update("[dim]Loading replies...[/dim]")
            status.display = True
        elif level.failed_message is not None:
            status.update(f"[bold red]{escape(level.failed_message)}[/bold red]")
            status.display = True
        elif not level.replies_loaded:
            status.update("[dim]Loading replies...[/dim]")
            status.display = True
        elif not level.replies and level.next_cursor is None:
            status.update("No replies yet.")
            status.display = True
        else:
            status.display = False

    def load_more_replies(self) -> None:
        level = self.current_level
        if level.loading_replies or (level.replies_loaded and level.next_cursor is None):
            return
        if (
            level.replies_loaded
            and not self.query_one("#commit-replies", RepliesListView).is_vertical_scroll_end
        ):
            return
        cursor = level.next_cursor
        if cursor in level.requested_cursors:
            status = self.query_one("#replies-status", Static)
            status.update("[bold red]The replies API repeated a cursor; pagination stopped.[/bold red]")
            status.display = True
            return
        level.requested_cursors.add(cursor)
        level.loading_replies = True
        level.failed_cursor = None
        level.failed_message = None
        self._update_reply_status(level)
        generation = level.generation
        self.app.run_worker(
            self._load_replies_page(level, cursor, generation),
            group=f"commit-replies-{level.commit.id}",
            exclusive=True,
        )

    async def _load_replies_page(
        self,
        level: CommitThreadLevel,
        cursor: str | None,
        generation: int,
    ) -> None:
        try:
            page = await asyncio.to_thread(
                self.client.get_replies,
                level.commit.id,
                cursor,
            )
        except ApiError as error:
            if self.is_mounted and generation == level.generation:
                level.failed_cursor = cursor
                level.failed_message = f"Could not load replies: {error}"
        except OSError as error:
            if self.is_mounted and generation == level.generation:
                level.failed_cursor = cursor
                level.failed_message = f"Could not load replies: {error}"
        else:
            if not self.is_mounted or generation != level.generation:
                return
            level.replies_loaded = True
            level.next_cursor = page.next_cursor
            level.replies.extend(page.commits)
            if level is self.current_level:
                reply_list = self.query_one("#commit-replies", RepliesListView)
                reply_list.extend(
                    CommitCard(reply, thread_view=True)
                    for reply in page.commits
                )
                self._update_reply_status(level)
            if (
                level is self.current_level
                and level.next_cursor is not None
                and self.query_one("#commit-replies", RepliesListView).is_vertical_scroll_end
            ):
                self.app.call_after_refresh(self.app._load_more_commit_replies)
        finally:
            if generation == level.generation:
                level.loading_replies = False
            if (
                self.is_mounted
                and generation == level.generation
                and level is self.current_level
            ):
                self._update_reply_status(level)

    def action_retry_replies(self) -> None:
        level = self.current_level
        if level.failed_message is not None:
            level.requested_cursors.discard(level.failed_cursor)
            level.next_cursor = level.failed_cursor
            level.failed_cursor = None
            level.failed_message = None
            self.load_more_replies()

    def _move_reply_selection(self, direction: int) -> None:
        reply_list = self.query_one("#commit-replies", RepliesListView)
        count = len(reply_list.children)
        if count == 0:
            return
        index = reply_list.index
        if index is not None and self._scroll_reply_into_view(reply_list, index):
            return
        if index is None:
            index = 0 if direction > 0 else count - 1
        else:
            index = min(max(index + direction, 0), count - 1)
        reply_list.index = index
        reply_list.focus()
        self.current_level.selected_index = index

    @staticmethod
    def _scroll_reply_into_view(reply_list: RepliesListView, index: int) -> bool:
        reply = reply_list.children[index]
        viewport = reply_list.content_region
        if reply.region.height > viewport.height:
            return False
        if reply.region.y < viewport.y:
            scroll_delta = reply.region.y - viewport.y
        elif reply.region.bottom > viewport.bottom:
            scroll_delta = reply.region.bottom - viewport.bottom
        else:
            return False
        reply_list.scroll_to(
            y=reply_list.scroll_y + scroll_delta,
            animate=False,
            force=True,
        )
        return True

    def action_next_reply(self) -> None:
        if self.image_view_active:
            self.query_one("#thread-image-view", CommitImageViewer).pan(vertical=1)
        else:
            self._move_reply_selection(1)

    def action_previous_reply(self) -> None:
        if self.image_view_active:
            self.query_one("#thread-image-view", CommitImageViewer).pan(vertical=-1)
        else:
            self._move_reply_selection(-1)

    def action_next_image(self) -> None:
        if self.image_view_active:
            self.query_one("#thread-image-view", CommitImageViewer).next_image()

    def action_previous_image(self) -> None:
        if self.image_view_active:
            self.query_one("#thread-image-view", CommitImageViewer).previous_image()

    def action_zoom_in(self) -> None:
        if self.image_view_active:
            self.query_one("#thread-image-view", CommitImageViewer).zoom_in()

    def action_zoom_out(self) -> None:
        if self.image_view_active:
            self.query_one("#thread-image-view", CommitImageViewer).zoom_out()

    def action_pan_left(self) -> None:
        if self.image_view_active:
            self.query_one("#thread-image-view", CommitImageViewer).pan(horizontal=-1)

    def action_pan_right(self) -> None:
        if self.image_view_active:
            self.query_one("#thread-image-view", CommitImageViewer).pan(horizontal=1)

    def action_toggle_image_view(self) -> None:
        if not self.current_level.commit.images:
            self.app.notify("This commit has no attached images.", severity="information")
            return
        self.image_view_active = not self.image_view_active
        mode = self.query_one("#thread-mode", ContentSwitcher)
        mode.current = "thread-image-view" if self.image_view_active else "thread-columns"
        self.query_one("#commit-detail-dialog", Container).set_class(
            self.image_view_active,
            "image-view-active",
        )
        viewer = self.query_one("#thread-image-view", CommitImageViewer)
        if self.image_view_active:
            viewer.activate()
            self.query_one("#detail-help", Static).update(self._image_view_help_text())
        else:
            viewer.deactivate()
            self.query_one("#detail-help", Static).update(self._detail_help_text())

    def _image_view_help_text(self) -> str:
        return (
            "[bold cyan]n[/] next · [bold cyan]Ctrl+N[/] previous · "
            "[bold cyan]= / -[/] zoom · [bold cyan]h / j / k / l[/] pan · "
            "[bold cyan]i[/] thread · [bold cyan]q / Esc[/] back"
        )

    def _detail_help_text(self) -> str:
        return (
            "Ctrl+j/k scroll ancestors · Alt+j/k scroll replying-to · "
            "j/k select reply · Enter follow · "
            "i images · "
            "Ctrl+R reply · u parent · q/Esc back · r retry"
        )

    def action_reply_to_current(self) -> None:
        level = self.current_level
        parent = level.commit
        reply_list = self.query_one("#commit-replies", RepliesListView)
        index = reply_list.index
        if index is not None and 0 <= index < len(level.replies):
            parent = level.replies[index]
        self.app._begin_composition(parent=parent)

    async def action_toggle_star_current(self) -> None:
        level = self.current_level
        commit = level.commit
        reply_list = self.query_one("#commit-replies", RepliesListView)
        index = reply_list.index
        if index is not None and 0 <= index < len(level.replies):
            commit = level.replies[index]
        self.app._queue_commit_star_toggle(commit)

    def update_commit(self, commit: ApiCommit) -> None:
        for level in self.levels:
            if level.commit.id == commit.id:
                level.commit = commit
            level.replies = [
                commit if reply.id == commit.id else reply
                for reply in level.replies
            ]
        self._show_current_level()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.list_view, RepliesListView) or not isinstance(event.item, CommitCard):
            return
        self.current_level.selected_index = event.index
        self.levels.append(CommitThreadLevel(event.item.commit))
        self._show_current_level()
        self.load_more_replies()

    def reply_posted(self, parent: ApiCommit) -> None:
        level = self.current_level
        if level.commit.id != parent.id:
            index = next(
                (
                    index
                    for index, reply in enumerate(level.replies)
                    if reply.id == parent.id
                ),
                None,
            )
            if index is not None:
                level.selected_index = index
        level.generation += 1
        level.loading_replies = False
        level.replies.clear()
        level.next_cursor = None
        level.replies_loaded = False
        level.failed_cursor = None
        level.failed_message = None
        level.requested_cursors.clear()
        self._show_current_level()
        self.load_more_replies()

    async def action_parent_comment(self) -> None:
        if len(self.levels) > 1:
            self.levels.pop()
            self._show_current_level()
            return
        if self.parent_navigation_loading:
            return
        if not self.ancestor_context:
            self.app.notify("This commit has no parent comment.", severity="information")
            return

        parent = self.ancestor_context[-1]
        if parent.deleted:
            self.app.notify("The parent comment has been deleted.", severity="warning")
            return

        current_commit_id = self.current_level.commit.id
        self.parent_navigation_loading = True
        try:
            detail = await asyncio.to_thread(self.client.get_commit_detail, parent.id)
        except (ApiError, OSError) as error:
            if self.is_mounted:
                self.app.notify(f"Could not open the parent comment: {error}", severity="warning")
        else:
            if (
                not self.is_mounted
                or self.current_level.commit.id != current_commit_id
            ):
                return
            self.levels = [CommitThreadLevel(detail.commit)]
            self.ancestor_context = detail.ancestors
            self._show_current_level()
            self.load_more_replies()
        finally:
            self.parent_navigation_loading = False

    async def action_back(self) -> None:
        await self.app.pop_screen()


def _relative_age(created_at: str) -> str:
    try:
        committed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return created_at
    if committed.tzinfo is None:
        committed = committed.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - committed).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _commit_display(
    commit: ApiCommit,
    body_limit: int,
    *,
    show_counts: bool = False,
    width: int | None = None,
    thread_view: bool = False,
    include_quote_text: bool = False,
) -> Text:
    body = commit.body.strip()
    if len(body) > body_limit:
        body = f"{body[: body_limit - 3].rstrip()}..."
    author = commit.author_name or commit.username
    rendered = Text()
    rendered.append(author, style="bold")
    has_quoted_commit = (
        commit.quoted_commit_id is not None or commit.quoted_commit is not None
    )
    rendered.append(
        f" @{commit.username} · {_relative_age(commit.created_at)}",
        style="dim",
    )
    if thread_view and has_quoted_commit:
        rendered.append(" · quoted reply", style="bold cyan")
    rendered.append("\n")
    rendered.append(_linkify_urls(body))
    if include_quote_text and has_quoted_commit:
        _append_inline_quote(rendered, commit.quoted_commit)
    if show_counts:
        rendered.append(
            f"\n★ {commit.stars_count}",
            style="bold yellow" if commit.starred else "dim",
        )
        rendered.append(f"   ↩ {commit.replies_count}", style="dim")
    if commit.images:
        image_count = len(commit.images)
        label = "image attached" if image_count == 1 else f"{image_count} images attached"
        rendered.append(f"\n📎 {label}", style="dim")
    if has_quoted_commit and not thread_view:
        _append_commit_preview(
            rendered,
            "QUOTED COMMIT",
            commit.quoted_commit,
            "Original commit is unavailable.",
            width=max(1, width or 72),
        )
    return rendered


def _append_inline_quote(
    rendered: Text,
    preview: ApiCommitPreview | None,
) -> None:
    rendered.append("\n\n↳ ", style="bold cyan")
    if preview is None:
        rendered.append("Original commit is unavailable.", style="dim")
        return

    author = preview.author_name or preview.username
    if author is not None:
        rendered.append(author, style="bold")
        if preview.username is not None:
            rendered.append(f" @{preview.username}", style="dim")
        rendered.append(": ", style="dim")

    if preview.deleted:
        body = "This commit was deleted."
    elif preview.body is None or not preview.body.strip():
        body = "Commit text is unavailable."
    else:
        body = preview.body.strip()
        if len(body) > 180:
            body = f"{body[:177].rstrip()}..."

    rendered.append("“", style="dim")
    rendered.append(_linkify_urls(body))
    rendered.append("”", style="dim")


def _append_commit_preview(
    rendered: Text,
    label: str,
    preview: ApiCommitPreview | None,
    unavailable_message: str,
    *,
    width: int,
) -> None:
    top_prefix = "╭─ " if width >= 3 else "╭"
    inner_width = max(1, width - cell_len(top_prefix))
    header_lines = Text(label, style="bold cyan").wrap(
        Console(width=inner_width, force_terminal=False),
        inner_width,
        overflow="fold",
    )
    rendered.append("\n\n", style="dim")
    for index, line in enumerate(header_lines):
        rendered.append(
            top_prefix if index == 0 else "│ " if width >= 2 else "│",
            style="dim",
        )
        rendered.append(line)
    if preview is None:
        _append_quoted_line(rendered, Text(unavailable_message, style="dim"), width)
    else:
        author = preview.author_name or preview.username
        if author is not None:
            author_line = Text(author, style="bold")
            if preview.username is not None:
                author_line.append(f" @{preview.username}", style="dim")
            _append_quoted_line(rendered, author_line, width)
        if preview.deleted:
            body = "This commit was deleted."
        elif preview.body is None or not preview.body.strip():
            body = "Commit text is unavailable."
        else:
            body = preview.body.strip()
            if len(body) > 180:
                body = f"{body[:177].rstrip()}..."
        for line in body.splitlines() or [body]:
            _append_quoted_line(rendered, _linkify_urls(line), width)
    rendered.append("\n╰─" if width >= 2 else "\n╰", style="dim")


def _append_quoted_line(rendered: Text, content: Text, width: int) -> None:
    prefix = "│ " if width >= 2 else "│"
    content_width = max(1, width - cell_len(prefix))
    wrapped_lines = content.wrap(
        Console(width=content_width, force_terminal=False),
        content_width,
        overflow="fold",
    )
    for line in wrapped_lines or [Text("")]:
        rendered.append(f"\n{prefix}", style="dim")
        rendered.append(line)


def _commit_context_display(commit: ApiCommitContext) -> Text:
    if commit.deleted or commit.body is None or commit.username is None:
        return Text("Deleted or unavailable commit", style="dim")
    rendered = Text()
    rendered.append(commit.author_name or commit.username, style="bold")
    rendered.append(
        f" @{commit.username} · {_relative_age(commit.created_at or '')}\n",
        style="dim",
    )
    rendered.append(_linkify_urls(commit.body.strip()))
    rendered.append(f"\n↩ {commit.replies_count}", style="dim")
    return rendered


def _notification_display(
    notification: ApiNotification,
    *,
    width: int | None = None,
) -> Text:
    messages = {
        "followed": "followed you",
        "replied": "replied to your commit",
        "mentioned": "mentioned you",
        "approved": "your commit was approved",
        "rejected": "your commit was rejected",
        "quoted": "quoted your commit",
        "starred": "starred your commit",
        "leveled_up": "reached a new rank",
    }
    rendered = Text()
    if notification.unread_count:
        rendered.append("NEW  ", style="bold cyan")
    if notification.actor is not None:
        rendered.append(f"@{notification.actor.username}", style="bold")
        if notification.actors_count > 1:
            rendered.append(f" and {notification.actors_count - 1} others")
        rendered.append(" ")
    rendered.append(
        messages.get(notification.type, f"notification: {notification.type.replace('_', ' ')}")
    )
    if notification.event_count > 1:
        rendered.append(f" · {notification.event_count} events", style="dim")
    rendered.append(f"\n{_relative_age(notification.created_at)} ago", style="dim")
    if notification.commit is not None and notification.commit.body.strip():
        rendered.append("\n")
        excerpt = notification.commit.body.strip()
        if len(excerpt) > 240:
            excerpt = f"{excerpt[:237].rstrip()}..."
        rendered.append(_linkify_urls(excerpt))
    if notification.commit is not None:
        if (
            notification.commit.quoted_commit_id is not None
            or notification.commit.quoted_commit is not None
        ):
            _append_commit_preview(
                rendered,
                "QUOTED COMMIT",
                notification.commit.quoted_commit,
                "Original commit is unavailable.",
                width=max(1, width or 72),
            )
    if notification.reason:
        reason = notification.reason.strip()
        if len(reason) > 240:
            reason = f"{reason[:237].rstrip()}..."
        rendered.append(f"\nReason: {reason}", style="dim")
    return rendered


class DevNullApp(App[None]):
    """Browse the public /dev/null logs from the terminal."""

    TITLE = "/dev/null"
    SUB_TITLE = "A social network for developers"
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        ("h", "previous_journal", "Previous journal"),
        ("j", "next_commit", "Next commit"),
        ("k", "previous_commit", "Previous commit"),
        ("l", "next_journal", "Next journal"),
        ("r", "refresh_log", "Refresh journal"),
        ("s", "toggle_star_selected", "Toggle star"),
        Binding("C", "new_commit", "New commit", key_display="Shift+C"),
        Binding("shift+c", "new_commit", show=False),
        Binding("Q", "quote_selected", "Quote", key_display="Shift+Q"),
        Binding("shift+q", "quote_selected", show=False),
        Binding("R", "reply_selected", "Reply", key_display="Shift+R"),
        Binding("shift+r", "reply_selected", show=False),
        Binding("n", "show_notifications", "Notifications"),
        Binding("ctrl+p", "submit_or_command_palette", show=False, priority=True),
        Binding("ctrl+q", "close_dialog_or_quit", show=False, priority=True),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.authenticated_user: ApiUser | None = None
        self.api_client: SignedApiClient | None = None
        self.pending_authorization: DeviceAuthorization | None = None
        self.active_log: LogName = "latest"
        self.log_cursors: dict[LogName, str | None] = {
            "following": None,
            "latest": None,
            "trending": None,
        }
        self.log_loaded: dict[LogName, bool] = {
            "following": False,
            "latest": False,
            "trending": False,
        }
        self.log_refresh_generation = 0
        self.commit_star_toggles: set[str] = set()
        self.log_versions: dict[LogName, int] = {
            "following": 0,
            "latest": 0,
            "trending": 0,
        }
        self.log_loading: dict[LogName, bool] = {
            "following": False,
            "latest": False,
            "trending": False,
        }
        self.notification_cursor: str | None = None
        self.notifications_version = 0
        self.notifications_loading = False

    def on_mount(self) -> None:
        self.set_interval(0.25, self._poll_callback_handoff)
        self.run_worker(self._restore_saved_session(), group="session-restore", exclusive=True)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="app-body"):
            yield self._sidebar()
            with Vertical(id="main-column"):
                with Container(id="toolbar"):
                    yield Label("LOG / LATEST", id="view-title")
                    yield Input(placeholder="Search commits...", id="search")
                with ContentSwitcher(initial="latest-view", id="views"):
                    yield self._log_view("latest")
                    yield self._log_view("following")
                    yield self._log_view("trending")
                    yield self._notifications_view()
                    yield self._profile_view()
        yield Footer()

    def _sidebar(self) -> Container:
        return Container(
            Static("[bold cyan]/dev/null[/bold cyan]\n[dim]developer logs[/dim]", id="brand"),
            Button("Latest log", id="latest-log", variant="primary"),
            Button("Following", id="following-log"),
            Button("Trending", id="trending-log"),
            Button("Notifications", id="notifications"),
            Button("My profile", id="profile"),
            Static(
                "\n[dim]Sign in with GitHub to browse journals,\npost commits, and reply.[/dim]",
                id="sidebar-note",
            ),
            id="sidebar",
        )

    def _log_view(self, log_name: LogName) -> Container:
        commits = JournalListView(log_name)
        status = Static(
            "Sign in to load your /dev/null journals.",
            id=f"{log_name}-status",
            classes="log-status",
        )
        status.display = False
        return Container(
            status,
            commits,
            id=f"{log_name}-view",
        )

    def _notifications_view(self) -> Container:
        status = Static(
            "Sign in to load your notifications.",
            id="notifications-status",
            classes="log-status",
        )
        status.display = False
        return Container(
            status,
            Static(
                "Opening this inbox marks notifications as read on /dev/null.",
                id="notifications-note",
            ),
            NotificationsListView(id="notifications-list"),
            id="notifications-view",
        )

    def _profile_view(self) -> Container:
        callback_input = Input(placeholder="Paste the full devnull://auth/callback URL", id="auth-callback")
        callback_input.display = False
        complete_button = Button("Complete sign-in", id="complete-sign-in")
        complete_button.display = False
        logout_button = Button("Log out", id="logout")
        logout_button.display = False
        return Container(
            Static(
                "[bold]@guest[/bold]\n[dim]Public visitor[/dim]\n\n"
                "You are reading /dev/null as a guest.\n"
                "GitHub identity keeps developer profiles real.",
                classes="profile-card",
                id="profile-card",
            ),
            Button("Sign in with GitHub", variant="primary", id="sign-in"),
            logout_button,
            callback_input,
            complete_button,
            Static("", id="sign-in-status"),
            id="profile",
        )

    def action_show_feed(self) -> None:
        self._show_log("latest")

    def action_show_following(self) -> None:
        self._show_log("following")

    def action_show_trending(self) -> None:
        self._show_log("trending")

    def action_show_notifications(self) -> None:
        self._show_view("notifications-view", "NOTIFICATIONS", "notifications")
        self._start_notifications_load(reset=True)

    def action_previous_journal(self) -> None:
        self._move_journal(-1)

    def action_next_journal(self) -> None:
        self._move_journal(1)

    def action_next_commit(self) -> None:
        self._move_commit_selection(1)

    def action_previous_commit(self) -> None:
        self._move_commit_selection(-1)

    def action_new_commit(self) -> None:
        self._begin_composition()

    def action_reply_selected(self) -> None:
        self._begin_reply()

    def action_quote_selected(self) -> None:
        self._begin_quote()

    async def action_close_dialog_or_quit(self) -> None:
        if isinstance(self.screen, CommitDetailScreen):
            await self.screen.action_back()
        elif isinstance(self.screen, (CommitComposerScreen, NotificationDetailScreen)):
            await self.pop_screen()
        else:
            self.exit()

    async def action_submit_or_command_palette(self) -> None:
        if isinstance(self.screen, CommitComposerScreen):
            await self._submit_commit()
        else:
            self.action_command_palette()

    def action_show_profile(self) -> None:
        title = (
            f"PROFILE / @{self.authenticated_user.username}"
            if self.authenticated_user is not None
            else "PROFILE / GUEST"
        )
        self._show_view("profile", title, "profile")

    def _show_view(self, view: str, title: str, active_button: str) -> None:
        self.query_one("#views", ContentSwitcher).current = view
        self.query_one("#view-title", Label).update(title)
        self.query_one("#search", Input).display = view != "notifications-view"
        for button_id in (
            "latest-log",
            "following-log",
            "trending-log",
            "notifications",
            "profile",
        ):
            is_active = button_id == active_button
            self.query_one(f"#{button_id}", Button).variant = (
                "primary" if is_active else "default"
            )
            self.query_one(f"#{button_id}", Button).set_class(is_active, "active")

    def _show_log(self, log_name: LogName) -> None:
        self.active_log = log_name
        self.log_refresh_generation += 1
        refresh_generation = self.log_refresh_generation
        titles = {
            "following": "LOG / FOLLOWING",
            "latest": "LOG / LATEST",
            "trending": "LOG / TRENDING",
        }
        self._show_view(f"{log_name}-view", titles[log_name], f"{log_name}-log")
        if self.log_loaded[log_name]:
            self.run_worker(
                self._refresh_log_after_idle(log_name, refresh_generation),
                group="log-auto-refresh",
                exclusive=True,
            )
        else:
            self._start_log_load(log_name, reset=True)

    async def _refresh_log_after_idle(
        self,
        log_name: LogName,
        refresh_generation: int,
    ) -> None:
        await asyncio.sleep(2)
        if (
            self.is_running
            and self.log_refresh_generation == refresh_generation
            and self.active_log == log_name
            and self.query_one("#views", ContentSwitcher).current == f"{log_name}-view"
        ):
            self._start_log_load(log_name, reset=True)

    def action_refresh_log(self) -> None:
        if self.query_one("#views", ContentSwitcher).current != f"{self.active_log}-view":
            return
        self.log_refresh_generation += 1
        self._start_log_load(self.active_log, reset=True)

    def action_toggle_star_selected(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        current_view = self.query_one("#views", ContentSwitcher).current
        if current_view not in {"latest-view", "following-view", "trending-view"}:
            return
        if self.api_client is None:
            self.notify("Sign in before starring a commit.", severity="warning")
            return
        commit = self._selected_commit()
        if commit is not None:
            self._queue_commit_star_toggle(commit)

    def _queue_commit_star_toggle(self, commit: ApiCommit) -> None:
        if self.api_client is None:
            self.notify("Sign in before starring a commit.", severity="warning")
            return
        if commit.id in self.commit_star_toggles:
            return
        self.commit_star_toggles.add(commit.id)
        self.run_worker(
            self._toggle_commit_star(commit),
            group=f"commit-star-{commit.id}",
        )

    async def _toggle_commit_star(self, commit: ApiCommit) -> None:
        client = self.api_client
        if client is None:
            self.notify("Sign in before starring a commit.", severity="warning")
            self.commit_star_toggles.discard(commit.id)
            return

        try:
            if commit.starred is None:
                detail = await asyncio.to_thread(client.get_commit_detail, commit.id)
                commit = detail.commit
                if commit.starred is None:
                    raise ApiError("The API did not return this commit's star state.")
            updated = await asyncio.to_thread(
                client.set_commit_star,
                commit.id,
                not commit.starred,
            )
        except (ApiError, OSError) as error:
            self.notify(f"Could not toggle star: {error}", severity="warning")
        else:
            if updated.starred is None:
                self.notify(
                    "The API did not confirm the updated star state.",
                    severity="warning",
                )
            else:
                self._update_commit_surfaces(updated)
                self.notify("Star added." if updated.starred else "Star removed.")
        finally:
            self.commit_star_toggles.discard(commit.id)

    def _update_commit_surfaces(self, commit: ApiCommit) -> None:
        for log_name in ("following", "latest", "trending"):
            list_view = self.query_one(f"#{log_name}-list", JournalListView)
            for item in list_view.children:
                if isinstance(item, CommitCard) and item.commit.id == commit.id:
                    item.update_commit(commit)

        notification_list = self.query_one("#notifications-list", NotificationsListView)
        for item in notification_list.children:
            if (
                isinstance(item, NotificationCard)
                and item.notification.commit is not None
                and item.notification.commit.id == commit.id
            ):
                item.update_commit(commit)

        if isinstance(self.screen, CommitDetailScreen):
            self.screen.update_commit(commit)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "latest-log": self.action_show_feed,
            "following-log": self.action_show_following,
            "trending-log": self.action_show_trending,
            "notifications": self.action_show_notifications,
            "profile": self.action_show_profile,
        }
        action = actions.get(event.button.id)
        if action is not None:
            action()
        elif event.button.id == "sign-in":
            await self._sign_in()
        elif event.button.id == "complete-sign-in":
            await self._complete_sign_in(self.query_one("#auth-callback", Input).value)
        elif event.button.id == "logout":
            await self._logout()
        elif event.button.id == "submit-commit":
            await self._submit_commit()
        elif event.button.id == "cancel-commit":
            await self._cancel_composition()
        elif event.button.id == "find-images":
            if isinstance(self.screen, CommitComposerScreen):
                await self.screen.action_find_images()
        elif event.button.id == "remove-image":
            if isinstance(self.screen, CommitComposerScreen):
                await self.screen.action_choose_images_to_remove()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if (
            isinstance(event.list_view, NotificationsListView)
            and isinstance(event.item, NotificationCard)
        ):
            commit = event.item.notification.commit
            if commit is None:
                self.push_screen(NotificationDetailScreen(event.item.notification))
            elif self.api_client is not None:
                self.push_screen(CommitDetailScreen(commit, self.api_client))
            else:
                self.notify("Sign in to open this notification.", severity="warning")
            return
        if not isinstance(event.list_view, JournalListView) or not isinstance(event.item, CommitCard):
            return
        if self.api_client is None:
            self.notify("Sign in to view commit replies.", severity="warning")
            return
        self.push_screen(CommitDetailScreen(event.item.commit, self.api_client))

    async def _restore_saved_session(self) -> None:
        button = self.query_one("#sign-in", Button)
        status = self.query_one("#sign-in-status", Static)
        button.disabled = True
        try:
            key_path = await asyncio.to_thread(existing_device_identity)
            if key_path is None:
                return
            client = SignedApiClient(key_path)
            user = await asyncio.to_thread(client.get_current_user)
        except ApiError as error:
            if error.code == "device_not_approved":
                status.update("[yellow]Saved device is not approved. Sign in again to authorize it.[/yellow]")
            else:
                status.update(f"[bold red]Could not restore sign-in: {escape(str(error))}[/bold red]")
        except OSError as error:
            status.update(f"[bold red]Could not restore sign-in: {escape(str(error))}[/bold red]")
        else:
            self._set_authenticated_user(client, user)
            status.update("[bold green]Signed in with the saved device.[/bold green]")
            self._start_log_load(self.active_log, reset=True)
        finally:
            if self.authenticated_user is None:
                button.disabled = False

    async def _sign_in(self) -> None:
        button = self.query_one("#sign-in", Button)
        callback_input = self.query_one("#auth-callback", Input)
        complete_button = self.query_one("#complete-sign-in", Button)
        status = self.query_one("#sign-in-status", Static)
        button.disabled = True
        button.label = "Preparing device..."
        status.update("[dim]Registering the callback handler and preparing your device...[/dim]")
        try:
            await asyncio.to_thread(register_uri_handler)
            authorization = await asyncio.to_thread(create_device_authorization)
            opened = await asyncio.to_thread(webbrowser.open, authorization.authorization_url)
        except ApiError as error:
            status.update(f"[bold red]{escape(str(error))}[/bold red]")
        except OSError as error:
            status.update(f"[bold red]Could not start sign-in: {escape(str(error))}[/bold red]")
        except webbrowser.Error as error:
            status.update(f"[bold red]Could not open the browser: {escape(str(error))}[/bold red]")
        else:
            self.pending_authorization = authorization
            callback_input.value = ""
            callback_input.display = True
            complete_button.display = True
            browser_status = "Browser opened." if opened else "Open this URL in your browser:"
            status.update(
                f"{browser_status} Approve the device. The callback handler is ready to return automatically; "
                "otherwise paste the full callback URL here.\n"
                f"[dim]{escape(authorization.authorization_url)}[/dim]"
            )
        finally:
            button.label = "Sign in with GitHub"
            button.disabled = False

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "auth-callback":
            await self._complete_sign_in(event.value)

    async def _complete_sign_in(self, callback_url: str) -> None:
        authorization = self.pending_authorization
        status = self.query_one("#sign-in-status", Static)
        if authorization is None:
            status.update("[bold red]Start sign-in before submitting a callback.[/bold red]")
            return

        try:
            code = authorization_code_from_callback(callback_url, authorization.state)
        except ApiError as error:
            status.update(f"[bold red]{escape(str(error))}[/bold red]")
            return

        self.pending_authorization = None
        complete_button = self.query_one("#complete-sign-in", Button)
        complete_button.disabled = True
        complete_button.label = "Redeeming code..."
        status.update("[dim]Redeeming the one-time authorization code...[/dim]")
        client = SignedApiClient(authorization.key_path)
        try:
            user = await asyncio.to_thread(client.redeem_device, code, authorization.code_verifier)
        except ApiError as error:
            status.update(f"[bold red]{escape(str(error))}[/bold red] Start sign-in again to retry.")
        except OSError as error:
            status.update(f"[bold red]Could not complete sign-in: {escape(str(error))}[/bold red]")
        else:
            self._set_authenticated_user(client, user)
            self.query_one("#auth-callback", Input).display = False
            complete_button.display = False
            status.update("[bold green]Signed in.[/bold green]")
            self._start_log_load(self.active_log, reset=True)
        finally:
            complete_button.label = "Complete sign-in"
            complete_button.disabled = False

    def _set_authenticated_user(self, client: SignedApiClient, user: ApiUser) -> None:
        self.api_client = client
        self.authenticated_user = user
        self.query_one("#profile-card", Static).update(
            f"[bold]@{escape(user.username)}[/bold]\n[dim]{escape(user.name or user.username)}[/dim]\n\n"
            f"{escape(user.bio or 'No bio set on GitHub.')}\n\n"
            "[dim]Signed in with your approved SSH device key.[/dim]"
        )
        self.query_one("#sign-in", Button).display = False
        self.query_one("#logout", Button).display = True
        if self.query_one("#views", ContentSwitcher).current == "profile":
            self.query_one("#view-title", Label).update(
                f"PROFILE / @{escape(user.username)}"
            )

    async def _logout(self) -> None:
        status = self.query_one("#sign-in-status", Static)
        button = self.query_one("#logout", Button)
        button.disabled = True
        status.update("[dim]Removing the saved local device key...[/dim]")
        try:
            await asyncio.to_thread(remove_device_identity)
        except ApiError as error:
            status.update(f"[bold red]Could not log out: {escape(str(error))}[/bold red]")
        except OSError as error:
            status.update(f"[bold red]Could not log out: {escape(str(error))}[/bold red]")
        else:
            self.authenticated_user = None
            self.api_client = None
            self.pending_authorization = None
            self.query_one("#profile-card", Static).update(
                "[bold]@guest[/bold]\n[dim]Public visitor[/dim]\n\n"
                "You are reading /dev/null as a guest.\n"
                "GitHub identity keeps developer profiles real."
            )
            self.query_one("#sign-in", Button).display = True
            button.display = False
            self.query_one("#auth-callback", Input).display = False
            self.query_one("#complete-sign-in", Button).display = False
            self.query_one("#notifications-list", NotificationsListView).clear()
            notifications_status = self.query_one("#notifications-status", Static)
            notifications_status.update("Sign in to load your notifications.")
            notifications_status.display = False
            self.notification_cursor = None
            self.notifications_version += 1
            self.notifications_loading = False
            for log_name in ("following", "latest", "trending"):
                self.query_one(f"#{log_name}-list", ListView).clear()
                self.query_one(f"#{log_name}-status", Static).update(
                    "Sign in to load your /dev/null journals."
                )
                self.query_one(f"#{log_name}-status", Static).display = False
                self.log_cursors[log_name] = None
                self.log_loaded[log_name] = False
                self.log_versions[log_name] += 1
                self.log_loading[log_name] = False
            self._show_view("profile", "PROFILE / GUEST", "profile")
            status.update(
                "[yellow]Signed out locally. Revoke this device in /settings/devices "
                "to remove its server-side approval.[/yellow]"
            )
        finally:
            button.disabled = False

    def _move_journal(self, direction: int) -> None:
        if self.query_one("#views", ContentSwitcher).current == "profile":
            return
        journals: tuple[LogName, ...] = ("latest", "following", "trending")
        current = journals.index(self.active_log)
        target = (current + direction) % len(journals)
        self._show_log(journals[target])

    def _move_commit_selection(self, direction: int) -> None:
        current_view = self.query_one("#views", ContentSwitcher).current
        if current_view == "profile":
            return
        if current_view == "notifications-view":
            list_view = self.query_one("#notifications-list", NotificationsListView)
            count = len(list_view.children)
            if not count:
                return
            index = list_view.index
            if index is None:
                index = 0 if direction > 0 else count - 1
            else:
                index = min(max(index + direction, 0), count - 1)
            list_view.index = index
            list_view.focus()
            return
        list_view = self.query_one(f"#{self.active_log}-list", ListView)
        count = len(list_view.children)
        if count == 0:
            return
        index = list_view.index
        if index is None:
            index = 0 if direction > 0 else count - 1
        else:
            index = min(max(index + direction, 0), count - 1)
        list_view.index = index
        list_view.focus()

    def _begin_composition(
        self,
        parent: ApiCommit | None = None,
        quoted_commit: ApiCommit | None = None,
    ) -> None:
        if self.api_client is None:
            self.notify("Sign in before posting a commit or reply.", severity="warning")
            return
        if self.query_one("#views", ContentSwitcher).current == "profile":
            return
        if parent is not None:
            mode = "reply"
            context = parent
        elif quoted_commit is not None:
            mode = "quote"
            context = quoted_commit
        else:
            mode = "new"
            context = None
        self.push_screen(CommitComposerScreen(mode, context))

    def _load_more_commit_replies(self) -> None:
        if isinstance(self.screen, CommitDetailScreen):
            self.screen.load_more_replies()

    def _begin_reply(self) -> None:
        selected = self._selected_commit()
        if selected is not None:
            self._begin_composition(selected)

    def _begin_quote(self) -> None:
        selected = self._selected_commit()
        if selected is not None:
            self._begin_composition(quoted_commit=selected)

    def _selected_commit(self) -> ApiCommit | None:
        if self.api_client is None:
            return None
        list_view = self.query_one(f"#{self.active_log}-list", ListView)
        index = list_view.index
        if index is None or index < 0 or index >= len(list_view.children):
            self.notify("Select a commit before replying or quoting it.", severity="warning")
            return None
        selected = list_view.children[index]
        if not isinstance(selected, CommitCard):
            self.notify("Select a commit before replying or quoting it.", severity="warning")
            return None
        return selected.commit

    async def _cancel_composition(self) -> None:
        if isinstance(self.screen, CommitComposerScreen):
            await self.pop_screen()

    async def _submit_commit(self) -> None:
        screen = self.screen
        if not isinstance(screen, CommitComposerScreen):
            return
        client = self.api_client
        status = screen.query_one("#composer-status", Static)
        if client is None:
            status.update("[bold red]Sign in before posting.[/bold red]")
            return
        body = screen.query_one("#commit-body", TextArea).text
        if not body.strip():
            status.update("[bold red]A commit cannot be empty.[/bold red]")
            return

        reply_parent = screen.commit if screen.mode == "reply" else None
        parent_id = reply_parent.id if reply_parent is not None else None
        quoted_commit_id = screen.commit.id if screen.mode == "quote" and screen.commit else None
        button = screen.query_one("#submit-commit", Button)
        button.disabled = True
        status.update(
            "[dim]Submitting your signed image commit...[/dim]"
            if screen.image_paths
            else "[dim]Submitting your signed commit...[/dim]"
        )
        try:
            if screen.image_paths:
                await asyncio.to_thread(
                    client.create_commit_with_images,
                    body,
                    tuple(screen.image_paths),
                    parent_id,
                )
            else:
                await asyncio.to_thread(
                    client.create_commit,
                    body,
                    parent_id,
                    quoted_commit_id,
                )
        except ApiError as error:
            status.update(f"[bold red]Could not post: {escape(str(error))}[/bold red]")
        except OSError as error:
            status.update(f"[bold red]Could not post: {escape(str(error))}[/bold red]")
        else:
            confirmation = (
                "Reply posted."
                if parent_id is not None
                else "Quote posted."
                if quoted_commit_id is not None
                else "Commit posted."
            )
            await self.pop_screen()
            self.notify(confirmation)
            if reply_parent is not None and isinstance(self.screen, CommitDetailScreen):
                self.screen.reply_posted(reply_parent)
            else:
                self._start_log_load(self.active_log, reset=True)
        finally:
            if button.is_mounted:
                button.disabled = False

    def _start_log_load(self, log_name: LogName, reset: bool) -> None:
        status = self.query_one(f"#{log_name}-status", Static)
        if self.api_client is None:
            status.display = True
            status.update("Sign in to load your /dev/null journals.")
            return
        if self.log_loading[log_name]:
            return
        if not reset and self.log_cursors[log_name] is None:
            return
        cursor = None if reset else self.log_cursors[log_name]
        self.log_versions[log_name] += 1
        version = self.log_versions[log_name]
        self.log_loading[log_name] = True
        self.run_worker(
            self._load_log_page(log_name, cursor, reset, version),
            group=f"log-{log_name}",
            exclusive=True,
        )

    async def _load_log_page(
        self,
        log_name: LogName,
        cursor: str | None,
        reset: bool,
        version: int,
    ) -> None:
        client = self.api_client
        if client is None:
            return
        status = self.query_one(f"#{log_name}-status", Static)
        commit_list = self.query_one(f"#{log_name}-list", ListView)
        status.display = False
        try:
            log = await asyncio.to_thread(client.get_log, log_name, cursor)
        except ApiError as error:
            if version == self.log_versions[log_name] and self.is_running:
                status.display = True
                status.update(f"[bold red]Could not load journal: {escape(str(error))}[/bold red]")
        except OSError as error:
            if version == self.log_versions[log_name] and self.is_running:
                status.display = True
                status.update(f"[bold red]Could not load journal: {escape(str(error))}[/bold red]")
        else:
            if version != self.log_versions[log_name] or not self.is_running:
                return
            if reset:
                commit_list.clear()
            cards = [CommitCard(commit) for commit in log.commits]
            commit_list.extend(cards)
            self.log_cursors[log_name] = log.next_cursor
            self.log_loaded[log_name] = True
            if not cards and log.suggestions:
                names = ", ".join(f"@{user.username}" for user in log.suggestions)
                status.display = True
                status.update(f"No commits in Following yet. Suggested developers: {escape(names)}")
            elif not cards:
                status.display = True
                status.update("No commits in this journal yet.")
            else:
                status.display = False
            if log.next_cursor is not None:
                self.call_after_refresh(
                    self._prefill_log_if_needed,
                    log_name,
                    version,
                )
        finally:
            if version == self.log_versions[log_name]:
                self.log_loading[log_name] = False

    def _on_journal_scroll_end(self, log_name: LogName) -> None:
        if self.query_one("#views", ContentSwitcher).current != f"{log_name}-view":
            return
        self._start_log_load(log_name, reset=False)

    def _prefill_log_if_needed(self, log_name: LogName, version: int) -> None:
        if not self.is_running or version != self.log_versions[log_name]:
            return
        list_view = self.query_one(f"#{log_name}-list", JournalListView)
        if list_view.is_vertical_scroll_end:
            self._on_journal_scroll_end(log_name)

    def _start_notifications_load(self, reset: bool) -> None:
        status = self.query_one("#notifications-status", Static)
        notification_list = self.query_one("#notifications-list", NotificationsListView)
        if self.api_client is None:
            status.display = True
            status.update("Sign in to load your notifications.")
            return
        if self.notifications_loading:
            return
        if not reset and self.notification_cursor is None:
            return
        if reset:
            self.notification_cursor = None
            notification_list.clear()
        self.notifications_version += 1
        version = self.notifications_version
        self.notifications_loading = True
        status.display = True
        status.update("[dim]Loading notifications...[/dim]")
        self.run_worker(
            self._load_notifications_page(
                self.notification_cursor,
                reset,
                version,
            ),
            group="notifications",
            exclusive=True,
        )

    async def _load_notifications_page(
        self,
        cursor: str | None,
        reset: bool,
        version: int,
    ) -> None:
        client = self.api_client
        if client is None:
            return
        status = self.query_one("#notifications-status", Static)
        notification_list = self.query_one("#notifications-list", NotificationsListView)
        try:
            page = await asyncio.to_thread(client.get_notifications, cursor)
        except (ApiError, OSError) as error:
            if version == self.notifications_version and self.is_running:
                status.display = True
                status.update(
                    f"[bold red]Could not load notifications: {escape(str(error))}[/bold red]"
                )
        else:
            if version != self.notifications_version or not self.is_running:
                return
            if reset:
                notification_list.clear()
            cards = [NotificationCard(item) for item in page.notifications]
            notification_list.extend(cards)
            self.notification_cursor = page.next_cursor
            if not notification_list.children and page.next_cursor is None:
                status.display = True
                status.update("No notifications yet.")
            else:
                status.display = False
            if page.next_cursor is not None:
                self.call_after_refresh(self._prefill_notifications_if_needed, version)
        finally:
            if version == self.notifications_version:
                self.notifications_loading = False

    def _on_notifications_scroll_end(self) -> None:
        if self.query_one("#views", ContentSwitcher).current != "notifications-view":
            return
        self._start_notifications_load(reset=False)

    def _prefill_notifications_if_needed(self, version: int) -> None:
        if not self.is_running or version != self.notifications_version:
            return
        notification_list = self.query_one("#notifications-list", NotificationsListView)
        if notification_list.is_vertical_scroll_end:
            self._on_notifications_scroll_end()

    async def _poll_callback_handoff(self) -> None:
        authorization = self.pending_authorization
        if authorization is None:
            return
        callback_path = callback_handoff_path(authorization.state)
        try:
            if callback_path.is_symlink():
                callback_path.unlink()
                raise ApiError("The browser callback handoff was not a regular file.")
            if not callback_path.is_file():
                return
            if callback_path.stat().st_size > 8192:
                callback_path.unlink()
                raise ApiError("The browser callback URL is too long.")
            callback_url = callback_path.read_text(encoding="utf-8")
            callback_path.unlink()
        except (OSError, ApiError) as error:
            status = self.query_one("#sign-in-status", Static)
            status.update(f"[bold red]{escape(str(error))}[/bold red]")
            return
        await self._complete_sign_in(callback_url)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search":
            return
        query = event.value.casefold().strip()
        for log_name in ("following", "latest", "trending"):
            for item in self.query_one(f"#{log_name}-list", ListView).query(CommitCard):
                item.display = not query or query in (
                    f"{item.commit.author_name or item.commit.username} "
                    f"@{item.commit.username} {item.commit.body}"
                ).casefold()


def main() -> None:
    """Run the TUI or handle a registered devnull:// callback."""

    parser = argparse.ArgumentParser(prog="dev-null-tty")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--callback", help=argparse.SUPPRESS)
    mode.add_argument("--register-handler", action="store_true", help="Register the per-user devnull:// handler")
    arguments = parser.parse_args()
    try:
        if arguments.callback:
            from .desktop import deliver_authorization_callback

            deliver_authorization_callback(arguments.callback)
            return
        if arguments.register_handler:
            print(register_uri_handler())
            return
    except ApiError as error:
        print(f"dev-null-tty: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    DevNullApp().run()


__all__ = ["Commit", "DevNullApp", "main"]
