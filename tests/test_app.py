import base64
import hashlib
import json
import os
import subprocess
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image as PILImage, ImageDraw
from rich.cells import cell_len
from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import MouseScrollDown
from textual.theme import Theme
from textual.widgets import Button, ContentSwitcher, Input, Label, Static, TextArea
from textual_image._terminal import get_cell_size

from dev_null_tty import (
    CommitDetailScreen,
    CommitComposerScreen,
    CommitImagePreview,
    CommitImageViewer,
    DevNullApp,
    LinkedText,
    LinkedText,
    NotificationDetailScreen,
    _commit_display,
    _image_crop_size,
    _linkify_urls,
    _load_omarchy_theme,
    _render_image_minimap,
    _terminal_ansi_theme,
)
from dev_null_tty.api import (
    ApiError,
    ApiCommit,
    ApiCommitContext,
    ApiCommitDetail,
    ApiCommitImage,
    ApiCommitPreview,
    ApiLog,
    ApiNotification,
    ApiNotifications,
    ApiUser,
    DeviceAuthorization,
    SignedApiClient,
    authorization_code_from_callback,
    create_device_authorization,
    existing_device_identity,
    remove_device_identity,
    resolve_ssh_identity,
    signed_headers,
)


@pytest.fixture(autouse=True)
def avoid_live_session_restore(monkeypatch) -> None:
    monkeypatch.setattr("dev_null_tty.existing_device_identity", lambda: None)
    monkeypatch.setattr("dev_null_tty.register_uri_handler", lambda: None)


@pytest.mark.asyncio
async def test_app_starts_on_latest_log() -> None:
    app = DevNullApp()
    async with app.run_test():
        assert app.query_one("#views", ContentSwitcher).current == "latest-view"
        assert str(app.query_one("#view-title", Label).render()) == "LOG / LATEST"
        bound_keys = {
            binding.key if isinstance(binding, Binding) else binding[0]
            for binding in app.BINDINGS
        }
        assert not any(key in {"1", "2", "3", "4"} for key in bound_keys)
        assert not app.query("#new-commit")
        assert not app.query("#reply-selected")
        assert not app.query("#quote-selected")
        assert not app.query("#search")


@pytest.mark.asyncio
async def test_new_commit_composer_expands_and_reserves_image_rows() -> None:
    app = DevNullApp()

    async with app.run_test(size=(100, 40)) as pilot:
        app.push_screen(CommitComposerScreen("new"))
        await pilot.pause()

        dialog = app.screen.query_one("#composer-dialog")
        text_area = app.screen.query_one("#commit-body", TextArea)
        images = app.screen.query_one("#composer-images", Static)

        assert "new-commit" in dialog.classes
        assert dialog.region.height >= 36
        assert text_area.region.height > 7
        assert images.region.height == 3


def test_load_omarchy_theme_reads_palette_colors(tmp_path) -> None:
    colors = {
        "accent": "#112233",
        "selection": "#223344",
        "muted": "#334455",
        "background": "#445566",
        "dark_background": "#556677",
        "darker_background": "#667788",
        "lighter_background": "#778899",
        "foreground": "#8899aa",
        "light_foreground": "#99aabb",
        "bright_foreground": "#aabbcc",
        "red": "#bbccdd",
        "yellow": "#ccddee",
        "orange": "#ddeeff",
        "green": "#eeff00",
        "cyan": "#ff0011",
        "blue": "#001122",
        "magenta": "#112244",
        "bright_red": "#223355",
        "bright_yellow": "#334466",
        "bright_green": "#445577",
        "bright_cyan": "#556688",
        "bright_blue": "#667799",
        "bright_magenta": "#7788aa",
    }
    colors_path = tmp_path / "colors.toml"
    colors_path.write_text(
        'mode = "light"\n'
        + "\n".join(f'{name} = "{value}"' for name, value in colors.items()),
        encoding="utf-8",
    )

    theme = _load_omarchy_theme(colors_path)

    assert theme is not None
    assert theme.name == "omarchy"
    assert theme.primary == colors["accent"]
    assert theme.foreground == colors["foreground"]
    assert theme.background == colors["background"]
    assert not theme.dark
    assert theme.variables["selection"] == colors["selection"]
    assert theme.variables["bright-foreground"] == colors["bright_foreground"]


def test_missing_omarchy_palette_uses_terminal_ansi_theme(tmp_path) -> None:
    assert _load_omarchy_theme(tmp_path / "missing-colors.toml") is None

    theme = _terminal_ansi_theme()

    assert theme.name == "terminal-ansi"
    assert theme.ansi
    assert theme.variables["selection"] == "ansi_blue"


@pytest.mark.asyncio
async def test_app_registers_and_uses_loaded_theme(monkeypatch) -> None:
    theme = _terminal_ansi_theme()
    theme.name = "test-palette"
    monkeypatch.setattr(
        "dev_null_tty._load_omarchy_theme",
        lambda _colors_path=None: theme,
    )

    app = DevNullApp()

    assert app.theme == "test-palette"
    async with app.run_test():
        assert app.theme == "test-palette"


@pytest.mark.asyncio
async def test_app_uses_terminal_theme_when_omarchy_palette_is_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "dev_null_tty._load_omarchy_theme",
        lambda _colors_path=None: None,
    )

    app = DevNullApp()

    assert app.theme == "terminal-ansi"
    async with app.run_test():
        assert app.theme == "terminal-ansi"


@pytest.mark.asyncio
async def test_ctrl_v_edits_commit_draft_in_nvim(monkeypatch) -> None:
    import dev_null_tty as dev_null_tty_module

    app = DevNullApp()
    monkeypatch.setattr(app, "suspend", nullcontext)
    monkeypatch.setattr(dev_null_tty_module.shutil, "which", lambda name: "/usr/bin/nvim")
    editor_files: list[Path] = []

    def fake_run(command: list[str], *, check: bool) -> subprocess.CompletedProcess:
        assert command[0] == "/usr/bin/nvim"
        assert check is False
        draft_path = Path(command[1])
        editor_files.append(draft_path)
        assert draft_path.read_text(encoding="utf-8") == "Existing draft"
        draft_path.write_text("Edited in Neovim", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(dev_null_tty_module.subprocess, "run", fake_run)

    async with app.run_test() as pilot:
        app.push_screen(CommitComposerScreen("new"))
        await pilot.pause()
        text_area = app.screen.query_one("#commit-body", TextArea)
        text_area.text = "Existing draft"
        await pilot.pause()

        await pilot.press("ctrl+v")
        await pilot.pause()

        assert text_area.text == "Edited in Neovim"
        assert app.screen.query_one("#submit-commit").disabled is False
        assert editor_files and not editor_files[0].exists()


@pytest.mark.asyncio
async def test_ctrl_v_reports_missing_nvim_without_changing_draft(monkeypatch) -> None:
    import dev_null_tty as dev_null_tty_module

    app = DevNullApp()
    monkeypatch.setattr(dev_null_tty_module.shutil, "which", lambda name: None)

    async with app.run_test() as pilot:
        app.push_screen(CommitComposerScreen("new"))
        await pilot.pause()
        text_area = app.screen.query_one("#commit-body", TextArea)
        text_area.text = "Keep this draft"
        await pilot.pause()

        await pilot.press("ctrl+v")

        assert text_area.text == "Keep this draft"
        assert "Install Neovim" in app.screen.query_one(
            "#composer-status", Static
        ).render().plain


@pytest.mark.asyncio
async def test_app_reloads_updated_omarchy_palette_without_restart(
    monkeypatch, tmp_path
) -> None:
    colors_path = tmp_path / "colors.toml"
    colors_path.write_text("initial-palette", encoding="utf-8")
    monkeypatch.setattr("dev_null_tty._omarchy_colors_path", lambda: colors_path)

    def palette(accent: str) -> Theme:
        theme = _terminal_ansi_theme()
        theme.name = "omarchy"
        theme.primary = accent
        return theme

    themes = {
        "initial-palette": palette("#123456"),
        "updated-palette-from-omarchy": palette("#abcdef"),
    }

    def load_palette(path: Path | None = None) -> Theme:
        return themes[(path or colors_path).read_text(encoding="utf-8")]

    monkeypatch.setattr("dev_null_tty._load_omarchy_theme", load_palette)
    app = DevNullApp()

    assert app.current_theme.primary == "#123456"

    async with app.run_test() as pilot:
        colors_path.write_text("updated-palette-from-omarchy", encoding="utf-8")
        await pilot.pause(0.75)

        assert app.current_theme.primary == "#abcdef"
        assert app.theme_variables["primary"] == "#ABCDEF"


@pytest.mark.asyncio
async def test_ctrl_p_opens_command_palette_outside_composer() -> None:
    app = DevNullApp()

    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        assert app.screen.id == "--command-palette"


@pytest.mark.asyncio
async def test_app_restores_saved_device_session(monkeypatch, tmp_path) -> None:
    key_path = tmp_path / "id_ed25519"

    class FakeClient:
        def __init__(self, saved_key_path) -> None:
            assert saved_key_path == key_path

        def get_current_user(self) -> ApiUser:
            return ApiUser("octocat", "The Octocat", None, "Building in public.")

        def get_log(self, log_name, cursor=None) -> ApiLog:
            return ApiLog((), None, log_name == "trending", 0, ())

    monkeypatch.setattr("dev_null_tty.existing_device_identity", lambda: key_path)
    monkeypatch.setattr("dev_null_tty.SignedApiClient", FakeClient)
    app = DevNullApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.authenticated_user is not None
        assert app.authenticated_user.username == "octocat"
        assert "octocat" in str(app.query_one("#profile-card", Static).render())
        assert not app.query_one("#sign-in").display


@pytest.mark.asyncio
async def test_navigation_switches_views() -> None:
    app = DevNullApp()
    async with app.run_test():
        app.action_show_following()
        assert app.query_one("#views", ContentSwitcher).current == "following-view"
        app.action_show_trending()
        assert app.query_one("#views", ContentSwitcher).current == "trending-view"
        app.action_show_notifications()
        assert app.query_one("#views", ContentSwitcher).current == "notifications-view"
        app.action_show_profile()
        assert app.query_one("#views", ContentSwitcher).current == "profile"


@pytest.mark.asyncio
async def test_journal_navigation_wraps_around() -> None:
    app = DevNullApp()
    async with app.run_test():
        app._move_journal(-1)
        assert app.active_log == "trending"
        app._move_journal(1)
        assert app.active_log == "latest"
        app._show_log("following")
        app._move_journal(1)
        assert app.active_log == "trending"
        app._move_journal(1)
        assert app.active_log == "latest"


@pytest.mark.asyncio
async def test_cached_journals_refresh_immediately_when_reselected() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        def get_log(self, log_name, cursor=None) -> ApiLog:
            self.calls.append((log_name, cursor))
            count = sum(name == log_name for name, _ in self.calls)
            commit = ApiCommit(
                f"{log_name}-{count}",
                "octocat",
                "The Octocat",
                f"{log_name} version {count}",
                "2026-10-20T18:00:00Z",
                0,
                0,
            )
            return ApiLog((commit,), None, False, 0, ())

    app = DevNullApp()
    client = FakeClient()
    app.api_client = client

    async with app.run_test() as pilot:
        async def wait_for_load(log_name: str, request_count: int) -> None:
            for _ in range(40):
                if (
                    sum(name == log_name for name, _ in client.calls) >= request_count
                    and not app.log_loading[log_name]
                ):
                    return
                await pilot.pause(0.05)
            pytest.fail(f"{log_name} did not finish loading")

        app._show_log("latest")
        await wait_for_load("latest", 1)
        app._show_log("following")
        await wait_for_load("following", 1)

        app._show_log("latest")
        assert app.query_one("#latest-list").children[0].commit.id == "latest-1"
        await wait_for_load("latest", 2)
        assert app.query_one("#latest-list").children[0].commit.id == "latest-2"

        app._show_log("following")
        assert app.query_one("#following-list").children[0].commit.id == "following-1"
        await wait_for_load("following", 2)
        assert app.query_one("#following-list").children[0].commit.id == "following-2"


@pytest.mark.asyncio
async def test_r_refreshes_active_journal() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def get_log(self, log_name, cursor=None) -> ApiLog:
            self.calls += 1
            return ApiLog(
                (
                    ApiCommit(
                        f"latest-{self.calls}",
                        "octocat",
                        "The Octocat",
                        f"Journal version {self.calls}",
                        "2026-10-20T18:00:00Z",
                        0,
                        0,
                    ),
                ),
                None,
                False,
                0,
                (),
            )

    app = DevNullApp()
    client = FakeClient()
    app.api_client = client

    async with app.run_test() as pilot:
        app._show_log("latest")
        for _ in range(20):
            if client.calls == 1 and not app.log_loading["latest"]:
                break
            await pilot.pause(0.05)

        app.query_one("#latest-list").focus()
        await pilot.press("r")
        for _ in range(20):
            if client.calls == 2 and not app.log_loading["latest"]:
                break
            await pilot.pause(0.05)

        assert client.calls == 2
        assert app.query_one("#latest-list").children[0].commit.id == "latest-2"


@pytest.mark.asyncio
async def test_s_toggles_star_in_log_and_commit_thread() -> None:
    initial_commit = ApiCommit(
        "star-target",
        "octocat",
        "The Octocat",
        "A commit to star.",
        "2026-10-20T18:00:00Z",
        0,
        0,
        starred=False,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.commit = initial_commit
            self.star_requests: list[tuple[str, bool]] = []

        def get_log(self, log_name, cursor=None) -> ApiLog:
            return ApiLog((self.commit,), None, False, 0, ())

        def get_commit_detail(self, commit_id):
            return ApiCommitDetail(self.commit, ())

        def get_replies(self, commit_id, cursor=None) -> ApiLog:
            return ApiLog((), None, False, 0, ())

        def set_commit_star(self, commit_id, starred) -> ApiCommit:
            self.star_requests.append((commit_id, starred))
            self.commit = ApiCommit(
                self.commit.id,
                self.commit.username,
                self.commit.author_name,
                self.commit.body,
                self.commit.created_at,
                self.commit.stars_count + (1 if starred else -1),
                self.commit.replies_count,
                starred=starred,
            )
            return self.commit

    app = DevNullApp()
    client = FakeClient()
    app.api_client = client

    async with app.run_test() as pilot:
        app._show_log("latest")
        for _ in range(20):
            if app.log_loaded["latest"] and not app.log_loading["latest"]:
                break
            await pilot.pause(0.05)

        journal = app.query_one("#latest-list")
        journal.index = 0
        journal.focus()
        await pilot.press("s")
        for _ in range(20):
            if client.star_requests:
                break
            await pilot.pause(0.05)

        assert client.star_requests == [("star-target", True)]
        assert journal.children[0].commit.starred is True

        await pilot.press("enter")
        await pilot.pause(0.1)
        await pilot.press("s")
        for _ in range(20):
            if len(client.star_requests) == 2:
                break
            await pilot.pause(0.05)

        assert client.star_requests == [
            ("star-target", True),
            ("star-target", False),
        ]
        assert isinstance(app.screen, CommitDetailScreen)
        assert app.screen.current_level.commit.starred is False


@pytest.mark.asyncio
async def test_notifications_view_loads_pages_and_opens_commit() -> None:
    notification_commit = ApiCommit(
        "notification-commit",
        "octocat",
        "The Octocat",
        "A commit from your notifications.",
        "2026-10-20T18:00:00Z",
        1,
        0,
    )
    notification = ApiNotification(
        "notification-id",
        "starred",
        "2026-10-20T18:00:00Z",
        ApiUser("octocat", "The Octocat", None, None),
        1,
        1,
        1,
        None,
        notification_commit,
    )
    follow_notification = ApiNotification(
        "follow-notification-id",
        "followed",
        "2026-10-20T18:00:00Z",
        ApiUser("octocat", "The Octocat", None, None),
        1,
        1,
        0,
        None,
        None,
    )

    class FakeClient:
        cursors = []

        def get_commit_detail(self, commit_id):
            if commit_id == "nearest-id":
                return ApiCommitDetail(
                    ApiCommit(
                        "nearest-id",
                        "nearest",
                        "Nearest",
                        "Nearest ancestor.",
                        "2026-10-20T17:30:00Z",
                        0,
                        1,
                    ),
                    (
                        ApiCommitContext(
                            "oldest-id",
                            "root",
                            "Root",
                            "Oldest ancestor.",
                            "2026-10-20T16:00:00Z",
                            2,
                        ),
                        ApiCommitContext(
                            "middle-id",
                            "middle",
                            "Middle",
                            "Middle ancestor.",
                            "2026-10-20T17:00:00Z",
                            1,
                        ),
                    ),
                )
            return ApiCommitDetail(
                notification_commit,
                (
                    ApiCommitContext(
                        "oldest-id",
                        "root",
                        "Root",
                        "Oldest ancestor.",
                        "2026-10-20T16:00:00Z",
                        2,
                    ),
                    ApiCommitContext(
                        "middle-id",
                        "middle",
                        "Middle",
                        "Middle ancestor.",
                        "2026-10-20T17:00:00Z",
                        1,
                    ),
                    ApiCommitContext(
                        "nearest-id",
                        "nearest",
                        "Nearest",
                        "Nearest ancestor.",
                        "2026-10-20T17:30:00Z",
                        1,
                    ),
                ),
            )

        def get_notifications(self, cursor=None):
            self.cursors.append(cursor)
            if cursor is None:
                return ApiNotifications(
                    (notification, follow_notification),
                    "older",
                    "2026-10-20T18:00:00Z",
                )
            return ApiNotifications((), None, "2026-10-20T18:00:00Z")

        def get_replies(self, commit_id, cursor=None) -> ApiLog:
            return ApiLog((), None, False, 0, ())

    app = DevNullApp()
    client = FakeClient()

    async with app.run_test(size=(120, 40)) as pilot:
        app._set_authenticated_user(
            client,
            ApiUser("reader", "Reader", None, None),
        )
        app.action_show_notifications()
        for _ in range(20):
            if len(client.cursors) == 2 and not app.notifications_loading:
                break
            await pilot.pause(0.05)

        notification_list = app.query_one("#notifications-list")
        assert client.cursors == [None, "older"]
        assert len(notification_list.children) == 2
        rendered_notification = notification_list.children[0].query_one(LinkedText).content
        assert isinstance(rendered_notification, Text)
        assert "starred your commit" in rendered_notification.plain

        await pilot.press("j", "j")
        assert notification_list.index == 1
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, NotificationDetailScreen)
        assert "followed you" in app.screen.query_one(
            "#notification-detail-content",
            LinkedText,
        ).content.plain

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, NotificationDetailScreen)
        await pilot.press("k")
        assert notification_list.index == 0
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, CommitDetailScreen)
        assert app.screen.current_level.commit == notification_commit
        thread_text = app.screen.query_one("#thread-context", LinkedText).content
        current_commit = app.screen.query_one("#current-commit", LinkedText).content
        assert isinstance(thread_text, Text)
        assert isinstance(current_commit, Text)
        assert "Oldest ancestor." in thread_text.plain
        assert "Middle ancestor." in thread_text.plain
        assert "Nearest ancestor." in thread_text.plain
        assert "A commit from your notifications." in current_commit.plain
        assert "↓" not in thread_text.plain
        await pilot.press("u")
        await pilot.pause(0.1)
        assert isinstance(app.screen, CommitDetailScreen)
        assert app.screen.current_level.commit.id == "nearest-id"
        await pilot.press("q")
        await pilot.pause()
        assert not isinstance(app.screen, CommitDetailScreen)


@pytest.mark.asyncio
async def test_commit_attachments_are_indicators_and_image_view_navigates() -> None:
    image_file = BytesIO()
    source_image = PILImage.new("RGB", (800, 600))
    draw = ImageDraw.Draw(source_image)
    for index in range(8):
        draw.rectangle(
            (index * 100, 0, index * 100 + 99, 599),
            fill=(index * 32, 255 - index * 32, index * 16),
        )
    source_image.save(image_file, format="PNG")
    images = (
        ApiCommitImage(
            "image-1",
            "https://dev-null.sh/commit-images/image-1",
            16,
            8,
            "First image",
        ),
        ApiCommitImage(
            "image-2",
            "https://dev-null.sh/commit-images/image-2",
            16,
            8,
            "Second image",
        ),
    )
    commit = ApiCommit(
        "image-commit",
        "octocat",
        "The Octocat",
        "A commit with a photo.",
        "2026-10-20T18:00:00Z",
        0,
        0,
        images=images,
    )
    requested_images = []

    class FakeClient:
        def get_log(self, log_name, cursor=None) -> ApiLog:
            return ApiLog((commit,), None, False, 0, ())

        def get_commit_image(self, requested_image) -> bytes:
            requested_images.append(requested_image.id)
            return image_file.getvalue()

        def get_commit_detail(self, commit_id) -> ApiCommitDetail:
            return ApiCommitDetail(commit, ())

        def get_replies(self, commit_id, cursor=None) -> ApiLog:
            return ApiLog((), None, False, 0, ())

    app = DevNullApp()
    client = FakeClient()
    app.api_client = client

    async with app.run_test() as pilot:
        app._show_log("latest")
        for _ in range(20):
            cards = app.query("#latest-list CommitCard")
            if cards:
                break
            await pilot.pause(0.05)
        assert cards
        card = cards[0]
        card_text = card.query_one(LinkedText).content
        assert isinstance(card_text, Text)
        assert "📎 2 images attached" in card_text.plain
        assert not card.query(CommitImagePreview)

        app.push_screen(CommitDetailScreen(commit, client))
        await pilot.pause(0.1)
        detail = app.screen
        mode = detail.query_one("#thread-mode", ContentSwitcher)
        assert mode.current == "thread-columns"
        viewer = detail.query_one("#thread-image-view", CommitImageViewer)

        await pilot.press("i")
        assert mode.current == "thread-image-view"
        for _ in range(20):
            preview = viewer.query_one(CommitImagePreview)
            if preview._loaded:
                break
            await pilot.pause(0.05)
        assert preview._loaded
        assert requested_images == ["image-1"]
        assert preview.source_image.size == (800, 600)
        assert "IMAGE 1 / 2" in viewer.query_one("#thread-image-caption", Label).render().plain
        assert not viewer.query("#thread-image-help")
        assert viewer.zoom_step == 0
        terminal_image = preview.query_one(".terminal-image")
        await pilot.pause(0.1)
        original_pixels = terminal_image.image.tobytes()
        original_widget_size = terminal_image.size
        cell_size = get_cell_size()
        frame_size = (original_widget_size.width, original_widget_size.height)
        cell_pixels = (cell_size.width, cell_size.height)
        frame_pixels = (
            frame_size[0] * cell_pixels[0],
            frame_size[1] * cell_pixels[1],
        )
        assert original_widget_size.width > 0
        assert original_widget_size.height > 0
        assert terminal_image.image.size == frame_pixels
        assert terminal_image.image.getpixel((0, 0)) == (16, 20, 24)
        assert terminal_image.image.getpixel(
            (frame_pixels[0] // 2, frame_pixels[1] // 2)
        ) != (16, 20, 24)
        assert terminal_image.render() is not None
        minimap = viewer.query_one("#thread-image-minimap", Static)
        minimap_panel = viewer.query_one("#thread-image-map")
        image_help = detail.query_one("#detail-help", Static)
        assert not minimap_panel.display
        image_stage = viewer.query_one("#thread-image-content")
        detail_dialog = detail.query_one("#commit-detail-dialog")
        assert "image-view-active" in detail_dialog.classes
        assert image_help.display
        assert "h / j / k / l pan" in image_help.render().plain
        assert "Ctrl+N previous" in image_help.render().plain

        await pilot.press("=")
        assert viewer.zoom_step == 1
        assert terminal_image.size == original_widget_size
        assert terminal_image.image.size == frame_pixels
        assert minimap_panel.display
        assert preview.region.y == image_stage.region.y
        assert preview.region.height == image_stage.region.height
        assert preview.region == image_stage.region
        assert terminal_image.region.width < preview.region.width
        assert terminal_image.region.height < preview.region.height
        assert terminal_image.region.x > preview.region.x
        assert terminal_image.region.y > preview.region.y
        assert detail_dialog.region.width == app.size.width
        assert minimap_panel.region.x == image_stage.region.x
        assert minimap_panel.region.y == image_stage.region.y
        assert minimap.region.width == 15
        assert minimap.region.height == 7
        assert minimap.region.x + minimap.region.width <= (
            minimap_panel.region.x + minimap_panel.region.width - 1
        )
        assert minimap.region.y + minimap.region.height <= (
            minimap_panel.region.y + minimap_panel.region.height - 1
        )
        minimap_before_pan = minimap.render().plain
        centered_zoom_pixels = terminal_image.image.tobytes()
        assert centered_zoom_pixels != original_pixels
        await pilot.press("l", "j")
        assert viewer.pan_x > 0.5
        assert viewer.pan_y > 0.5
        assert minimap.render().plain != minimap_before_pan
        assert terminal_image.image.tobytes() != centered_zoom_pixels
        edge_color = (app.current_theme.error or "#ff5c5c").removeprefix("ansi_")
        centered_minimap = _render_image_minimap(
            source_image,
            viewer.zoom_factor,
            0.5,
            0.5,
            frame_size,
            cell_pixels,
            edge_color=edge_color,
        )
        edge_red = Color.parse(edge_color)
        assert not any(span.style.color == edge_red for span in centered_minimap.spans)
        await pilot.press("l", "j", "l", "j", "l", "j")
        crop_width, crop_height = _image_crop_size(
            source_image.size,
            viewer.zoom_factor,
            frame_size,
            cell_pixels,
        )
        right_edge_pan = 1 - crop_width / (2 * 800) + round(crop_width * 0.1) / 800
        bottom_edge_pan = 1 - crop_height / (2 * 600) + round(crop_height * 0.1) / 600
        assert viewer.pan_x == pytest.approx(right_edge_pan)
        assert viewer.pan_y == pytest.approx(bottom_edge_pan)
        assert any(
            span.style.color == edge_red
            for span in _render_image_minimap(
                source_image,
                viewer.zoom_factor,
                viewer.pan_x,
                viewer.pan_y,
                frame_size,
                cell_pixels,
                edge_color=edge_color,
            ).spans
        )
        edge_pixels = terminal_image.image.tobytes()
        edge_minimap = minimap.render().plain
        rendered_edge_minimap = _render_image_minimap(
            source_image,
            viewer.zoom_factor,
            viewer.pan_x,
            viewer.pan_y,
            frame_size,
            cell_pixels,
            edge_color=edge_color,
        )
        assert any(span.style.color == edge_red for span in rendered_edge_minimap.spans)
        assert edge_minimap.count("┌") == 1
        assert edge_minimap.count("┐") == 1
        assert edge_minimap.count("└") == 1
        assert edge_minimap.count("┘") == 1
        assert edge_minimap.count("·") == 1
        assert "▀" in edge_minimap
        assert all(len(row) == 15 for row in edge_minimap.splitlines())
        assert len(edge_minimap.splitlines()) == 7
        await pilot.press("l", "j")
        assert terminal_image.image.tobytes() == edge_pixels
        assert minimap.render().plain == edge_minimap
        await pilot.press("h", "k", "h", "k", "h", "k", "h", "k")
        left_edge_pan = crop_width / (2 * 800) - round(crop_width * 0.1) / 800
        top_edge_pan = crop_height / (2 * 600) - round(crop_height * 0.1) / 600
        assert viewer.pan_x == pytest.approx(left_edge_pan)
        assert viewer.pan_y == pytest.approx(top_edge_pan)
        opposite_edge_pixels = terminal_image.image.tobytes()
        opposite_edge_minimap = minimap.render().plain
        await pilot.press("h", "k")
        assert terminal_image.image.tobytes() == opposite_edge_pixels
        assert minimap.render().plain == opposite_edge_minimap
        await pilot.press("-")
        assert viewer.zoom_step == 0
        assert not minimap_panel.display
        assert viewer.pan_x == 0.5
        assert viewer.pan_y == 0.5
        assert terminal_image.image.size == frame_pixels
        assert terminal_image.image.tobytes() == original_pixels

        await pilot.press("n")
        for _ in range(20):
            if preview._loaded and preview.image_info == images[1]:
                break
            await pilot.pause(0.05)
        assert preview._loaded
        assert preview.image_info == images[1]
        assert requested_images == ["image-1", "image-2"]

        await pilot.press("ctrl+n")
        for _ in range(20):
            if preview._loaded and preview.image_info == images[0]:
                break
            await pilot.pause(0.05)
        assert preview._loaded
        assert preview.image_info == images[0]

        await pilot.press("i")
        assert mode.current == "thread-columns"
        assert "image-view-active" not in detail_dialog.classes


@pytest.mark.asyncio
async def test_short_journal_auto_loads_until_it_fills_viewport() -> None:
    class FakeClient:
        cursors = []

        def get_log(self, log_name, cursor=None) -> ApiLog:
            self.cursors.append(cursor)
            commit = ApiCommit(
                f"commit-{len(self.cursors)}",
                "octocat",
                "The Octocat",
                f"Journal entry {len(self.cursors)}",
                "2026-10-20T18:00:00Z",
                0,
                0,
            )
            next_cursor = "page-2" if cursor is None else None
            return ApiLog((commit,), next_cursor, False, 0, ())

    app = DevNullApp()
    app.api_client = FakeClient()

    async with app.run_test(size=(120, 50)) as pilot:
        app._show_log("latest")
        for _ in range(20):
            if (
                len(app.query("#latest-list CommitCard")) == 2
                and not app.log_loading["latest"]
            ):
                break
            await pilot.pause(0.05)

        assert app.api_client.cursors == [None, "page-2"]
        assert len(app.query("#latest-list CommitCard")) == 2


@pytest.mark.asyncio
async def test_scrolling_to_end_loads_next_journal_page() -> None:
    class FakeClient:
        cursors = []

        def get_log(self, log_name, cursor=None) -> ApiLog:
            self.cursors.append(cursor)
            if cursor is None:
                commits = tuple(
                    ApiCommit(
                        f"commit-{index}",
                        "octocat",
                        "The Octocat",
                        f"Journal entry {index}",
                        "2026-10-20T18:00:00Z",
                        0,
                        0,
                    )
                    for index in range(20)
                )
                return ApiLog(commits, "page-2", False, 0, ())
            return ApiLog(
                (
                    ApiCommit(
                        "commit-next",
                        "octocat",
                        "The Octocat",
                        "Next page",
                        "2026-10-20T18:00:00Z",
                        0,
                        0,
                    ),
                ),
                None,
                False,
                0,
                (),
            )

    app = DevNullApp()
    app.api_client = FakeClient()

    async with app.run_test() as pilot:
        app._show_log("latest")
        await pilot.pause(0.1)
        list_view = app.query_one("#latest-list")
        assert len(app.api_client.cursors) == 1
        assert list_view.max_scroll_y > 0

        list_view.scroll_y = list_view.max_scroll_y
        await pilot.pause(0.1)

        assert app.api_client.cursors == [None, "page-2"]
        assert len(app.query("#latest-list CommitCard")) == 21


@pytest.mark.asyncio
async def test_vim_keys_navigate_journals_and_commits() -> None:
    class FakeClient:
        def get_log(self, log_name, cursor=None) -> ApiLog:
            commits = tuple(
                ApiCommit(
                    f"commit-{index}",
                    "octocat",
                    "The Octocat",
                    f"Entry {index}",
                    "2026-10-20T18:00:00Z",
                    0,
                    0,
                )
                for index in range(2)
            )
            return ApiLog(commits, None, False, 0, ())

    app = DevNullApp()

    async with app.run_test() as pilot:
        app._set_authenticated_user(
            FakeClient(),
            ApiUser("octocat", "The Octocat", None, None),
        )
        app._show_log("latest")
        await pilot.pause()
        await pilot.press("j")
        assert app.query_one("#latest-list").index == 0
        await pilot.press("j")
        assert app.query_one("#latest-list").index == 1
        await pilot.press("k")
        assert app.query_one("#latest-list").index == 0
        await pilot.press("l")
        assert app.query_one("#views", ContentSwitcher).current == "following-view"
        await pilot.press("h")
        assert app.query_one("#views", ContentSwitcher).current == "latest-view"


@pytest.mark.asyncio
async def test_user_can_submit_commits_and_replies() -> None:
    target = ApiCommit(
        "parent-id",
        "octocat",
        "The Octocat",
        "Original post",
        "2026-10-20T18:00:00Z",
        0,
        0,
    )

    class FakeClient:
        submissions = []

        def get_log(self, log_name, cursor=None) -> ApiLog:
            return ApiLog((target,), None, False, 0, ())

        def create_commit(
            self,
            body: str,
            parent_id: str | None = None,
            quoted_commit_id: str | None = None,
        ) -> ApiCommit:
            self.submissions.append((body, parent_id, quoted_commit_id))
            return ApiCommit(
                f"created-{len(self.submissions)}",
                "octocat",
                "The Octocat",
                body,
                "2026-10-20T18:00:00Z",
                0,
                0,
            )

    app = DevNullApp()
    client = FakeClient()

    async with app.run_test() as pilot:
        app._set_authenticated_user(client, ApiUser("octocat", "The Octocat", None, None))
        app._show_log("latest")
        await pilot.pause()
        app.query_one("#latest-list").index = 0
        await pilot.press("R")
        assert isinstance(app.screen, CommitComposerScreen)
        assert "Original post" in str(
            app.screen.query_one("#composer-context", Static).render()
        )
        post_button = app.screen.query_one("#submit-commit")
        text_area = app.screen.query_one("#commit-body", TextArea)
        assert post_button.disabled
        text_area.text = " \n "
        await pilot.pause()
        assert post_button.disabled
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert client.submissions == []
        assert isinstance(app.screen, CommitComposerScreen)
        assert "cannot be empty" in str(
            app.screen.query_one("#composer-status", Static).render()
        )
        text_area.text = "A thoughtful reply."
        await pilot.pause()
        assert not post_button.disabled
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert client.submissions[0] == ("A thoughtful reply.", "parent-id", None)
        assert not isinstance(app.screen, CommitComposerScreen)

        await pilot.pause()
        app.query_one("#latest-list").index = 0
        app.query_one("#latest-list").focus()
        await pilot.press("shift+q")
        assert isinstance(app.screen, CommitComposerScreen)
        assert "Original post" in str(
            app.screen.query_one("#composer-context", Static).render()
        )
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert not isinstance(app.screen, CommitComposerScreen)
        await pilot.press("Q")
        assert isinstance(app.screen, CommitComposerScreen)
        app.screen.query_one("#commit-body", TextArea).text = "A quote with my comment."
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert client.submissions[1] == ("A quote with my comment.", None, "parent-id")
        assert not isinstance(app.screen, CommitComposerScreen)

        app.query_one("#latest-list").focus()
        await pilot.press("C")
        assert isinstance(app.screen, CommitComposerScreen)
        app.screen.query_one("#commit-body", TextArea).text = "A new commit."
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert client.submissions[2] == ("A new commit.", None, None)
        assert not isinstance(app.screen, CommitComposerScreen)


@pytest.mark.asyncio
async def test_user_can_attach_images_to_a_new_commit(tmp_path, monkeypatch) -> None:
    from contextlib import nullcontext

    import dev_null_tty as dev_null_tty_module

    image_path = tmp_path / "photo.png"
    PILImage.new("RGB", (80, 40), (20, 40, 60)).save(image_path, format="PNG")
    picked_image_path = tmp_path / "picked.jpg"
    PILImage.new("RGB", (80, 40), (60, 40, 20)).save(picked_image_path, format="JPEG")
    target = ApiCommit(
        "existing",
        "octocat",
        "The Octocat",
        "An existing commit",
        "2026-10-20T18:00:00Z",
        0,
        0,
    )

    class FakeClient:
        image_submissions = []

        def get_log(self, log_name, cursor=None) -> ApiLog:
            return ApiLog((target,), None, False, 0, ())

        def create_commit_with_images(
            self,
            body: str,
            image_paths: tuple[Path, ...],
            parent_id: str | None = None,
        ) -> str:
            self.image_submissions.append((body, image_paths, parent_id))
            return "https://dev-null.sh/commits/created"

    app = DevNullApp()
    client = FakeClient()
    selections = [(image_path,), (picked_image_path,)]
    picker_calls = []

    def fake_picker(max_images, *, candidates=None, prompt="Images> "):
        picker_calls.append((max_images, candidates, prompt))
        if candidates is not None:
            return (image_path,)
        return selections.pop(0)

    monkeypatch.setattr(
        dev_null_tty_module,
        "_pick_images_with_fzf",
        fake_picker,
    )
    monkeypatch.setattr(app, "suspend", nullcontext)
    async with app.run_test() as pilot:
        app._set_authenticated_user(client, ApiUser("octocat", "The Octocat", None, None))
        app._show_log("latest")
        await pilot.pause()
        await pilot.press("C")
        assert isinstance(app.screen, CommitComposerScreen)
        assert not app.screen.query("#composer-image-path")
        assert not app.screen.query("#add-image")
        await pilot.press("ctrl+f")
        await pilot.pause()
        assert app.screen.image_paths == [image_path]
        await pilot.click("#find-images")
        await pilot.pause()
        assert app.screen.image_paths == [image_path, picked_image_path], str(
            app.screen.query_one("#composer-status", Static).render()
        )

        body = app.screen.query_one("#commit-body", TextArea)
        action_buttons = app.screen.query("#composer-actions Button")
        assert len(action_buttons) == 4
        assert not app.screen.query("#preview-images")
        assert len({button.region.y for button in action_buttons}) == 1
        body.focus()
        await pilot.press("i")
        assert body.text == "i"
        await pilot.press("ctrl+i")
        mode = app.screen.query_one("#composer-mode", ContentSwitcher)
        assert mode.current == "composer-image-view"
        dialog = app.screen.query_one("#composer-dialog")
        assert "image-view-active" in dialog.classes
        assert dialog.region.width == app.size.width
        assert dialog.region.height == app.size.height
        assert not app.screen.query_one("#composer-actions").display
        help_text = app.screen.query_one("#composer-help", Static).render()
        assert "Ctrl+N" in help_text.plain
        viewer = app.screen.query_one("#composer-image-view")
        preview = viewer.query_one(CommitImagePreview)
        for _ in range(20):
            if preview._loaded:
                break
            await pilot.pause(0.05)
        assert preview._loaded
        assert preview.source_image.size == (80, 40)
        await pilot.press("i")
        assert mode.current == "composer-image-view"
        await pilot.press("ctrl+i")
        assert mode.current == "composer-form"
        assert body.text == "i"

        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.screen.image_paths == [picked_image_path]
        assert picker_calls == [
            (3, None, "Images> "),
            (2, None, "Images> "),
            (2, (image_path, picked_image_path), "Remove images> "),
        ]

        body.text = "A commit with a photo."
        await pilot.press("ctrl+p")
        await pilot.pause()

        assert client.image_submissions == [
            ("A commit with a photo.", (picked_image_path,), None)
        ]
        assert not isinstance(app.screen, CommitComposerScreen)


@pytest.mark.asyncio
async def test_enter_opens_commit_detail_and_loads_paginated_replies() -> None:
    original_body = "Full original commit. " + ("additional context " * 50)
    ancestor_body = "Ancestor context. " + ("previous comment " * 30)
    parent = ApiCommit(
        "parent-id",
        "octocat",
        "The Octocat",
        original_body,
        "2026-10-20T18:00:00Z",
        0,
        12,
    )
    first_page = tuple(
        ApiCommit(
            f"reply-{index}",
            "octocat",
            "The Octocat",
            f"Reply {index}",
            "2026-10-20T18:00:00Z",
            0,
            0,
        )
        for index in range(12)
    )
    older_reply = ApiCommit(
        "older-reply",
        "octocat",
        "The Octocat",
        "An older reply.",
        "2026-10-20T18:00:00Z",
        0,
        0,
    )
    nested_replies = tuple(
        ApiCommit(
            f"nested-reply-{index}",
            "octocat",
            "The Octocat",
            f"Nested reply {index}",
            "2026-10-20T18:00:00Z",
            0,
            0,
        )
        for index in range(2)
    )

    class FakeClient:
        def __init__(self) -> None:
            self.reply_cursors = []

        def get_log(self, log_name, cursor=None) -> ApiLog:
            return ApiLog((parent,), None, False, 0, ())

        def get_commit_detail(self, commit_id):
            return ApiCommitDetail(
                parent,
                (
                    ApiCommitContext(
                        "ancestor-id",
                        "root",
                        "Root",
                        ancestor_body,
                        "2026-10-20T17:00:00Z",
                        1,
                    ),
                ),
            )

        def get_replies(self, commit_id, cursor=None) -> ApiLog:
            self.reply_cursors.append((commit_id, cursor))
            if commit_id == "reply-0":
                return ApiLog(nested_replies, None, False, 0, ())
            if commit_id != "parent-id":
                return ApiLog((), None, False, 0, ())
            if cursor is None:
                return ApiLog(first_page, "older", False, 0, ())
            return ApiLog((older_reply,), None, False, 0, ())

    app = DevNullApp()
    client = FakeClient()
    app.api_client = client

    async with app.run_test(size=(100, 32)) as pilot:
        app._show_log("latest")
        await pilot.pause(0.1)
        journal = app.query_one("#latest-list")
        journal.index = 0
        journal.focus()
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert isinstance(app.screen, CommitDetailScreen)
        history = app.screen.query_one("#thread-history-pane")
        thread_context = app.screen.query_one("#thread-context", LinkedText).content
        assert isinstance(thread_context, Text)
        current_commit = app.screen.query_one("#current-commit", LinkedText).content
        assert isinstance(current_commit, Text)
        assert original_body.strip() in current_commit.plain
        assert ancestor_body.strip() in thread_context.plain
        assert history.max_scroll_y > 0
        replies = app.screen.query_one("#commit-replies")
        assert history.region.x < replies.region.x
        assert history.region.right <= replies.region.x
        current_pane = app.screen.query_one("#current-commit-pane")
        assert current_pane.region.y < replies.region.y
        detail_dialog = app.screen.query_one("#commit-detail-dialog")
        assert detail_dialog.region.width >= app.size.width * 0.94
        assert detail_dialog.region.height >= app.size.height * 0.93
        assert current_pane.max_scroll_y > 0
        current_pane.scroll_to(
            y=current_pane.max_scroll_y,
            animate=False,
            force=True,
        )
        await pilot.pause()
        assert current_pane.scroll_y == current_pane.max_scroll_y
        current_pane.scroll_y = 0
        assert len(app.screen.current_level.replies) == 12
        assert len(replies.children) == 12
        assert client.reply_cursors == [("parent-id", None)]

        await pilot.press("j")
        assert current_pane.scroll_y == 0
        assert replies.index == 0
        history.scroll_y = 0
        await pilot._post_mouse_events(
            [MouseScrollDown],
            widget=history,
            offset=(3, 3),
        )
        assert history.scroll_y > 0
        history.scroll_y = 0
        await pilot.press("ctrl+j")
        assert history.scroll_y > 0
        await pilot.press("ctrl+k")
        assert history.scroll_y == 0
        current_pane.scroll_y = 0
        await pilot.press("alt+j")
        await pilot.pause()
        assert current_pane.scroll_y > 0
        await pilot.press("alt+k")
        await pilot.pause()
        assert current_pane.scroll_y == 0
        replies.scroll_y = replies.max_scroll_y
        await pilot.pause(0.1)
        assert client.reply_cursors == [("parent-id", None), ("parent-id", "older")]
        assert len(replies.children) == 13

        replies.index = 11
        await pilot.pause()
        selected_reply = replies.children[11]
        replies.scroll_y += 1
        await pilot.pause()
        assert (
            selected_reply.region.y < replies.content_region.y
            or selected_reply.region.bottom > replies.content_region.bottom
        )
        reply_fits = selected_reply.region.height <= replies.content_region.height
        await pilot.press("j")
        await pilot.pause()
        if reply_fits:
            assert replies.index == 11
            assert selected_reply.region.y >= replies.content_region.y
            assert selected_reply.region.bottom <= replies.content_region.bottom
            await pilot.press("j")
        assert replies.index == 12

        replies.index = 0
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert len(app.screen.levels) == 2
        assert client.reply_cursors[-1] == ("reply-0", None)
        thread_context = app.screen.query_one("#thread-context", LinkedText).content
        current_commit = app.screen.query_one("#current-commit", LinkedText).content
        assert isinstance(thread_context, Text)
        assert isinstance(current_commit, Text)
        assert original_body.strip() in thread_context.plain
        assert "Reply 0" in current_commit.plain
        current_pane = app.screen.query_one("#current-commit-pane")
        assert current_pane.max_scroll_y == 0
        nested_replies_list = app.screen.query_one("#commit-replies")
        await pilot.press("j", "j", "k")
        assert nested_replies_list.index == 0

        await pilot.press("u")
        await pilot.pause()
        assert len(app.screen.levels) == 1
        assert len(app.screen.query_one("#commit-replies").children) == 13
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, CommitDetailScreen)


@pytest.mark.asyncio
async def test_ctrl_r_replies_to_root_and_selected_comment() -> None:
    root = ApiCommit(
        "root-id",
        "octocat",
        "The Octocat",
        "Original post",
        "2026-10-20T18:00:00Z",
        0,
        1,
    )
    comment = ApiCommit(
        "comment-id",
        "octocat",
        "The Octocat",
        "A comment",
        "2026-10-20T18:00:00Z",
        0,
        0,
    )

    class FakeClient:
        submissions = []
        reply_requests = []

        def get_log(self, log_name, cursor=None) -> ApiLog:
            return ApiLog((root,), None, False, 0, ())

        def get_commit_detail(self, commit_id):
            return ApiCommitDetail(root, ())

        def get_replies(self, commit_id, cursor=None) -> ApiLog:
            self.reply_requests.append((commit_id, cursor))
            if commit_id == root.id:
                refreshed_comment = ApiCommit(
                    comment.id,
                    comment.username,
                    comment.author_name,
                    comment.body,
                    comment.created_at,
                    comment.stars_count,
                    sum(parent_id == comment.id for _, parent_id, _ in self.submissions),
                )
                return ApiLog((refreshed_comment,), None, False, 0, ())
            return ApiLog((), None, False, 0, ())

        def create_commit(
            self,
            body: str,
            parent_id: str | None = None,
            quoted_commit_id: str | None = None,
        ) -> ApiCommit:
            self.submissions.append((body, parent_id, quoted_commit_id))
            return ApiCommit(
                f"posted-{len(self.submissions)}",
                "octocat",
                "The Octocat",
                body,
                "2026-10-20T18:00:00Z",
                0,
                0,
            )

    app = DevNullApp()
    client = FakeClient()

    async with app.run_test() as pilot:
        app._set_authenticated_user(
            client,
            ApiUser("octocat", "The Octocat", None, None),
        )
        app._show_log("latest")
        await pilot.pause(0.1)
        app.query_one("#latest-list").index = 0
        app.query_one("#latest-list").focus()
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert isinstance(app.screen, CommitDetailScreen)
        await pilot.press("ctrl+r")
        assert isinstance(app.screen, CommitComposerScreen)
        assert app.screen.commit == root
        app.screen.query_one("#commit-body", TextArea).text = "Reply to the root."
        await pilot.press("ctrl+p")
        await pilot.pause(0.1)

        assert client.submissions == [("Reply to the root.", root.id, None)]
        assert isinstance(app.screen, CommitDetailScreen)
        assert client.reply_requests[-1] == (root.id, None)

        app.screen.query_one("#commit-replies").index = 0
        await pilot.press("ctrl+r")
        assert isinstance(app.screen, CommitComposerScreen)
        assert app.screen.commit == comment
        app.screen.query_one("#commit-body", TextArea).text = "Reply to the comment."
        await pilot.press("ctrl+p")
        await pilot.pause(0.1)

        assert client.submissions[1] == ("Reply to the comment.", comment.id, None)
        assert isinstance(app.screen, CommitDetailScreen)
        assert len(app.screen.levels) == 1
        assert app.screen.current_level.commit == root
        assert app.screen.current_level.selected_index == 0
        assert app.screen.current_level.replies[0].replies_count == 1
        assert client.reply_requests == [
            (root.id, None),
            (root.id, None),
            (root.id, None),
        ]


def test_commit_links_render_as_clickable_hyperlinks() -> None:
    commit = ApiCommit(
        "linked-id",
        "octocat",
        "The Octocat",
        "Visit https://example.com/docs (and https://dev-null.sh/).",
        "2026-10-20T18:00:00Z",
        0,
        0,
    )
    rendered = _commit_display(commit, 600, show_counts=True)

    links = [
        span.style.link
        for span in rendered.spans
        if isinstance(span.style, Style) and span.style.link is not None
    ]
    assert links == ["https://example.com/docs", "https://dev-null.sh/"]
    assert rendered.plain.endswith(" (and https://dev-null.sh/).\n★ 0   ↩ 0")


def test_commit_display_renders_quoted_commit_preview() -> None:
    commit = SignedApiClient._commit_from_payload(
        {
            "id": "quote-post",
            "author": {"username": "alice", "name": "Alice"},
            "body": "A post with context.",
            "created_at": "2026-10-20T18:00:00Z",
            "quoted_commit_id": "original-post",
            "quoted_commit": {
                "id": "original-post",
                "author": {"username": "bob", "name": "Bob"},
                "body": "The original idea.",
            },
        }
    )

    assert commit.quoted_commit == ApiCommitPreview(
        "original-post",
        "bob",
        "Bob",
        "The original idea.",
    )
    rendered = _commit_display(commit, 600, show_counts=True)

    assert "QUOTED COMMIT" in rendered.plain
    assert "Bob @bob" in rendered.plain
    assert "The original idea." in rendered.plain

    thread_rendered = _commit_display(
        commit,
        600,
        show_counts=True,
        thread_view=True,
    )
    thread_header = thread_rendered.plain.splitlines()[0]
    assert thread_header.startswith("Alice @alice · ")
    assert thread_header.endswith(" · quoted reply")
    assert "QUOTED COMMIT" not in thread_rendered.plain
    assert "The original idea." not in thread_rendered.plain

    replying_to_rendered = _commit_display(
        commit,
        600,
        show_counts=True,
        thread_view=True,
        include_quote_text=True,
    )
    assert "Alice @alice · " in replying_to_rendered.plain
    assert " · quoted reply" in replying_to_rendered.plain
    assert "Bob @bob: “The original idea.”" in replying_to_rendered.plain
    assert "QUOTED COMMIT" not in replying_to_rendered.plain


def test_commit_display_marks_deleted_and_unavailable_quote_targets() -> None:
    deleted_commit = SignedApiClient._commit_from_payload(
        {
            "id": "quote-post",
            "author": {"username": "alice"},
            "body": "A post with a deleted quote.",
            "created_at": "2026-10-20T18:00:00Z",
            "quoted_commit_id": "deleted-post",
            "quoted_commit": {
                "id": "deleted-post",
                "deleted_at": "2026-10-20T17:00:00Z",
            },
        }
    )
    unavailable_commit = SignedApiClient._commit_from_payload(
        {
            "id": "quote-post",
            "author": {"username": "alice"},
            "body": "A post with an unavailable quote.",
            "created_at": "2026-10-20T18:00:00Z",
            "quoted_commit_id": "private-post",
            "quoted_commit": None,
        }
    )

    assert "This commit was deleted." in _commit_display(
        deleted_commit, 600
    ).plain
    assert "Original commit is unavailable." in _commit_display(
        unavailable_commit, 600
    ).plain


def test_quoted_commit_lines_wrap_to_available_width() -> None:
    quote_text = "界" * 80 + " https://example.com/" + "long-segment" * 12
    commit = ApiCommit(
        "quote-post",
        "alice",
        "Alice",
        "A short post.",
        "2026-10-20T18:00:00Z",
        0,
        0,
        quoted_commit_id="original-post",
        quoted_commit=ApiCommitPreview(
            "original-post",
            "bob",
            "Bob",
            quote_text,
        ),
    )

    lines = _commit_display(commit, 600, width=24).plain.splitlines()
    quote_start = next(index for index, line in enumerate(lines) if "QUOTED COMMIT" in line)
    quote_lines = lines[quote_start:]

    assert all(cell_len(line) <= 24 for line in quote_lines)
    assert any("long-segment" in line for line in quote_lines)
    assert quote_lines[-1].startswith("╰")


@pytest.mark.asyncio
async def test_clicking_commit_link_opens_browser(monkeypatch) -> None:
    opened_urls = []

    def fake_open(url: str) -> bool:
        opened_urls.append(url)
        return True

    monkeypatch.setattr("dev_null_tty.webbrowser.open", fake_open)

    class LinkHarness(App[None]):
        def compose(self) -> ComposeResult:
            yield LinkedText(_linkify_urls("https://example.com"), id="commit-link")

    async with LinkHarness().run_test() as pilot:
        await pilot.click("#commit-link", offset=(5, 0))
        await pilot.pause()

    assert opened_urls == ["https://example.com"]


@pytest.mark.asyncio
async def test_sign_in_displays_authenticated_profile(monkeypatch, tmp_path) -> None:
    authorization = DeviceAuthorization(
        Path("/tmp/device-key"),
        "expected-state",
        "verifier",
        "https://dev-null.sh/mobile/authorize",
    )

    class FakeClient:
        def __init__(self, key_path) -> None:
            pass

        def redeem_device(self, code: str, code_verifier: str) -> ApiUser:
            assert code == "one-time-code"
            assert code_verifier == "verifier"
            return ApiUser("octocat", "The Octocat", None, "Building in public.")

        def get_log(self, log_name, cursor=None) -> ApiLog:
            return ApiLog((), None, log_name == "trending", 0, ())

    events = []

    def create_authorization():
        events.append("authorization")
        return authorization

    monkeypatch.setattr("dev_null_tty.register_uri_handler", lambda: events.append("register"))
    monkeypatch.setattr("dev_null_tty.create_device_authorization", create_authorization)
    monkeypatch.setattr("dev_null_tty.webbrowser.open", lambda url: True)
    monkeypatch.setattr("dev_null_tty.SignedApiClient", FakeClient)
    monkeypatch.setattr("dev_null_tty.api.DEVICE_DIRECTORY", tmp_path / ".dev-null-tui")
    app = DevNullApp()

    async with app.run_test():
        app.action_show_profile()
        await app._sign_in()
        assert events == ["register", "authorization"]
        from dev_null_tty.desktop import deliver_authorization_callback

        deliver_authorization_callback(
            "devnull://auth/callback?code=one-time-code&state=expected-state"
        )
        await app._poll_callback_handoff()
        assert app.authenticated_user is not None
        assert app.authenticated_user.username == "octocat"
        assert "octocat" in str(app.query_one("#profile-card", Static).render())


@pytest.mark.asyncio
async def test_sign_in_displays_device_approval_error(monkeypatch) -> None:
    authorization = DeviceAuthorization(
        Path("/tmp/device-key"),
        "expected-state",
        "verifier",
        "https://dev-null.sh/mobile/authorize",
    )

    class FakeClient:
        def __init__(self, key_path) -> None:
            pass

        def redeem_device(self, code: str, code_verifier: str) -> ApiUser:
            raise ApiError("Approve this key on the web first.", "device_not_approved")

    monkeypatch.setattr("dev_null_tty.create_device_authorization", lambda: authorization)
    monkeypatch.setattr("dev_null_tty.webbrowser.open", lambda url: True)
    monkeypatch.setattr("dev_null_tty.SignedApiClient", FakeClient)
    app = DevNullApp()

    async with app.run_test():
        app.action_show_profile()
        await app._sign_in()
        await app._complete_sign_in(
            "devnull://auth/callback?code=one-time-code&state=expected-state"
        )
        assert app.authenticated_user is None
        assert "Approve this key" in str(app.query_one("#sign-in-status", Static).render())


def test_signed_headers_use_canonical_payload_and_single_line_signature(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    import dev_null_tty.api as api

    monkeypatch.setattr(api.time, "time", lambda: 1_800_000_000)
    monkeypatch.setattr(
        api.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="12345678123456781234567812345678"),
    )

    def fake_sign(command, **kwargs) -> None:
        request_file = Path(command[-1])
        assert request_file.read_bytes() == (
            b"1800000000\n12345678123456781234567812345678\nGET\n/api/v1/me\n"
        )
        request_file.with_name(f"{request_file.name}.sig").write_text(
            "-----BEGIN SSH SIGNATURE-----\nYWJj\n-----END SSH SIGNATURE-----\n",
            encoding="ascii",
        )

    monkeypatch.setattr(api.subprocess, "run", fake_sign)
    headers = signed_headers("GET", "/api/v1/me", b"", tmp_path / "identity")
    assert headers["X-SSH-Signature"] == "YWJj"
    assert "\n" not in headers["X-SSH-Signature"]


def test_signed_headers_cover_device_redemption_body(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    import dev_null_tty.api as api

    monkeypatch.setattr(api.time, "time", lambda: 1_800_000_000)
    monkeypatch.setattr(
        api.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="12345678123456781234567812345678"),
    )
    body = b'{"code":"grant","code_verifier":"verifier"}'

    def fake_sign(command, **kwargs) -> None:
        request_file = Path(command[-1])
        assert request_file.read_bytes() == (
            b"1800000000\n12345678123456781234567812345678\n"
            b"POST\n/api/v1/devices\n{\"code\":\"grant\",\"code_verifier\":\"verifier\"}"
        )
        request_file.with_name(f"{request_file.name}.sig").write_text(
            "-----BEGIN SSH SIGNATURE-----\nYWJj\n-----END SSH SIGNATURE-----\n",
            encoding="ascii",
        )

    monkeypatch.setattr(api.subprocess, "run", fake_sign)
    headers = signed_headers("POST", "/api/v1/devices", body, tmp_path / "identity")

    assert headers["X-SSH-Signature"] == "YWJj"


def test_api_requests_identify_the_client(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    monkeypatch.setattr(api, "signed_headers", lambda *args: {})

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def geturl(self) -> str:
            return "https://dev-null.sh/api/v1/me"

        def read(self) -> bytes:
            return b"{}"

    class Opener:
        request = None

        def open(self, request, timeout: int) -> Response:
            self.request = request
            return Response()

    client = SignedApiClient(tmp_path / "identity")
    opener = Opener()
    client._opener = opener

    client._request("GET", "/api/v1/me")

    assert opener.request.get_header("User-agent") == "dev-null-tty"


def test_device_key_is_created_with_private_permissions(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    device_dir = tmp_path / ".dev-null-tui"
    monkeypatch.setattr(api, "DEVICE_DIRECTORY", device_dir)
    monkeypatch.setattr(api.shutil, "which", lambda name: "/usr/bin/ssh-keygen")
    monkeypatch.setattr(api, "_device_fingerprint", lambda key_path: "SHA256:fingerprint")
    calls = []

    def fake_run(command, **kwargs) -> None:
        calls.append(command)
        private_key = Path(command[-1])
        private_key.write_text("private-key", encoding="ascii")
        private_key.with_suffix(".pub").write_text(
            "ssh-ed25519 AAAA test\n",
            encoding="ascii",
        )

    monkeypatch.setattr(api.subprocess, "run", fake_run)

    key_path = resolve_ssh_identity()
    resolve_ssh_identity()

    assert key_path == device_dir / "id_ed25519"
    assert device_dir.stat().st_mode & 0o777 == 0o700
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert len(calls) == 1
    assert calls[0][calls[0].index("-t") + 1] == "ed25519"


def test_existing_device_identity_does_not_create_a_key(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    device_dir = tmp_path / ".dev-null-tui"
    monkeypatch.setattr(api, "DEVICE_DIRECTORY", device_dir)

    assert existing_device_identity() is None
    assert not device_dir.exists()


def test_existing_device_identity_returns_saved_key(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    device_dir = tmp_path / ".dev-null-tui"
    device_dir.mkdir()
    key_path = device_dir / "id_ed25519"
    key_path.write_text("private-key", encoding="ascii")
    key_path.with_suffix(".pub").write_text("ssh-ed25519 AAAA test\n", encoding="ascii")
    monkeypatch.setattr(api, "DEVICE_DIRECTORY", device_dir)
    monkeypatch.setattr(api, "_device_fingerprint", lambda path: "SHA256:fingerprint")

    assert existing_device_identity() == key_path
    assert key_path.stat().st_mode & 0o777 == 0o600


def test_remove_device_identity_leaves_other_app_data(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    device_dir = tmp_path / ".dev-null-tui"
    device_dir.mkdir()
    private_key = device_dir / "id_ed25519"
    public_key = device_dir / "id_ed25519.pub"
    private_key.write_text("private", encoding="ascii")
    public_key.write_text("public", encoding="ascii")
    handoff = device_dir / "pending-callback"
    handoff.write_text("callback", encoding="ascii")
    monkeypatch.setattr(api, "DEVICE_DIRECTORY", device_dir)

    remove_device_identity()

    assert not private_key.exists()
    assert not public_key.exists()
    assert handoff.read_text(encoding="ascii") == "callback"


def test_device_authorization_builds_s256_url(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    verifier = "v" * 43
    monkeypatch.setattr(api, "resolve_ssh_identity", lambda: tmp_path / "id_ed25519")
    monkeypatch.setattr(api, "_device_fingerprint", lambda key_path: "SHA256:fingerprint")
    tokens = iter((verifier, "expected-state"))
    monkeypatch.setattr(api.secrets, "token_urlsafe", lambda length: next(tokens))

    authorization = create_device_authorization()
    query = parse_qs(urlsplit(authorization.authorization_url).query)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    assert authorization.state == "expected-state"
    assert authorization.code_verifier == verifier
    assert query["fingerprint"] == ["SHA256:fingerprint"]
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["expected-state"]
    assert query["redirect_uri"] == ["devnull://auth/callback"]


def test_callback_validates_state_and_returns_code() -> None:
    callback = "devnull://auth/callback?code=grant-code&state=expected-state"

    assert authorization_code_from_callback(callback, "expected-state") == "grant-code"
    with pytest.raises(ApiError, match="state did not match"):
        authorization_code_from_callback(callback, "other-state")


def test_desktop_handler_delivers_callback_to_secure_handoff(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api
    from dev_null_tty.desktop import callback_handoff_path, deliver_authorization_callback

    monkeypatch.setattr(api, "DEVICE_DIRECTORY", tmp_path / ".dev-null-tui")
    callback = "devnull://auth/callback?code=grant-code&state=expected-state"

    handoff = deliver_authorization_callback(callback)

    assert handoff == callback_handoff_path("expected-state")
    assert handoff.read_text(encoding="utf-8") == callback
    assert handoff.stat().st_mode & 0o777 == 0o600


def test_uri_handler_registration_creates_xdg_desktop_entry(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    import dev_null_tty.desktop as desktop

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(desktop.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)

    desktop_file = desktop.register_uri_handler()
    contents = desktop_file.read_text(encoding="utf-8")

    assert "MimeType=x-scheme-handler/devnull;" in contents
    assert " -m dev_null_tty --callback %u" in contents
    assert "NoDisplay=true" not in contents
    assert "TryExec=" not in contents
    assert desktop.HANDLER_MARKER in contents
    assert calls[0][1:3] == ["query", "default"]
    assert calls[1][0].endswith("update-desktop-database")
    assert calls[2][1:3] == ["default", desktop.DESKTOP_ID]


def test_device_redemption_posts_signed_json(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    signed = {}

    def fake_signed_headers(method, path, body, key_path):
        signed.update(method=method, path=path, body=body)
        return {"X-SSH-Signature": "signature"}

    monkeypatch.setattr(api, "signed_headers", fake_signed_headers)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def geturl(self) -> str:
            return "https://dev-null.sh/api/v1/devices"

        def read(self) -> bytes:
            return b'{"data":{"username":"octocat","name":"The Octocat"}}'

    class Opener:
        request = None

        def open(self, request, timeout: int) -> Response:
            self.request = request
            return Response()

    client = SignedApiClient(tmp_path / "id_ed25519")
    opener = Opener()
    client._opener = opener

    user = client.redeem_device("grant-code", "v" * 43)

    assert user.username == "octocat"
    assert signed["method"] == "POST"
    assert signed["path"] == "/api/v1/devices"
    assert json.loads(signed["body"]) == {
        "code": "grant-code",
        "code_verifier": "v" * 43,
    }
    assert opener.request.get_header("Content-type") == "application/json"


def test_get_log_sends_cursor_and_parses_journal(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    signed = {}
    monkeypatch.setattr(
        api,
        "signed_headers",
        lambda method, path, body, key_path: signed.update(
            method=method, path=path, body=body
        )
        or {},
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def geturl(self) -> str:
            return "https://dev-null.sh/api/v1/log/trending?cursor=next%2Fpage"

        def read(self) -> bytes:
            return json.dumps(
                {
                    "data": [
                        {
                            "id": "commit-id",
                            "body": "A real journal entry",
                            "created_at": "2026-10-20T18:00:00Z",
                            "stars_count": 7,
                            "replies_count": 2,
                            "images": [
                                {
                                    "id": "image-id",
                                    "url": "https://dev-null.sh/commit-images/image-id",
                                    "width": 640,
                                    "height": 480,
                                    "description": "A photo",
                                }
                            ],
                            "author": {
                                "username": "octocat",
                                "name": "The Octocat",
                            },
                        }
                    ],
                    "meta": {
                        "next_cursor": "more",
                        "is_ranked": True,
                        "new_commits_count": 0,
                    },
                }
            ).encode("utf-8")

    class Opener:
        request = None

        def open(self, request, timeout: int) -> Response:
            self.request = request
            return Response()

    client = SignedApiClient(tmp_path / "id_ed25519")
    opener = Opener()
    client._opener = opener

    journal = client.get_log("trending", "next/page")

    assert signed == {
        "method": "GET",
        "path": "/api/v1/log/trending?cursor=next%2Fpage",
        "body": b"",
    }
    assert journal.next_cursor == "more"
    assert journal.is_ranked
    assert journal.commits[0] == ApiCommit(
        "commit-id",
        "octocat",
        "The Octocat",
        "A real journal entry",
        "2026-10-20T18:00:00Z",
        7,
        2,
        images=(
            ApiCommitImage(
                "image-id",
                "https://dev-null.sh/commit-images/image-id",
                640,
                480,
                "A photo",
            ),
        ),
    )


def test_get_commit_image_downloads_only_from_the_api_origin(tmp_path) -> None:
    class Headers:
        def get_content_type(self) -> str:
            return "image/webp"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def geturl(self) -> str:
            return "https://dev-null.sh/commit-images/image-id"

        def read(self, size: int) -> bytes:
            assert size == 10_240_001
            return b"webp-bytes"

    class Opener:
        request = None

        def open(self, request, timeout: int) -> Response:
            self.request = request
            assert timeout == 20
            return Response()

    client = SignedApiClient(tmp_path / "device-key")
    opener = Opener()
    client._opener = opener
    image = ApiCommitImage(
        "image-id",
        "https://dev-null.sh/commit-images/image-id",
        640,
        480,
        None,
    )

    assert client.get_commit_image(image) == b"webp-bytes"
    assert opener.request.full_url == image.url

    image = ApiCommitImage(
        "image-id",
        "https://example.com/image.webp",
        640,
        480,
        None,
    )
    with pytest.raises(ApiError, match="unexpected origin"):
        client.get_commit_image(image)


def test_get_replies_sends_signed_cursor_and_parses_replies(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    signed = {}
    monkeypatch.setattr(
        api,
        "signed_headers",
        lambda method, path, body, key_path: signed.update(
            method=method, path=path, body=body
        )
        or {},
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def geturl(self) -> str:
            return (
                "https://dev-null.sh/api/v1/commits/commit%2Fid/replies"
                "?newest=1&cursor=next%2Fpage"
            )

        def read(self) -> bytes:
            return json.dumps(
                {
                    "data": [
                        {
                            "id": "reply-id",
                            "body": "A useful reply.",
                            "created_at": "2026-10-20T18:00:00Z",
                            "stars_count": 1,
                            "replies_count": 0,
                            "author": {
                                "username": "octocat",
                                "name": "The Octocat",
                            },
                        }
                    ],
                    "meta": {"next_cursor": "older"},
                }
            ).encode("utf-8")

    class Opener:
        def open(self, request, timeout: int) -> Response:
            return Response()

    client = SignedApiClient(tmp_path / "id_ed25519")
    client._opener = Opener()

    replies = client.get_replies("commit/id", "next/page")

    assert signed == {
        "method": "GET",
        "path": "/api/v1/commits/commit%2Fid/replies?newest=1&cursor=next%2Fpage",
        "body": b"",
    }
    assert replies.next_cursor == "older"
    assert replies.commits == (
        ApiCommit(
            "reply-id",
            "octocat",
            "The Octocat",
            "A useful reply.",
            "2026-10-20T18:00:00Z",
            1,
            0,
        ),
    )


def test_get_notifications_sends_signed_cursor_and_parses_groups(
    monkeypatch,
    tmp_path,
) -> None:
    import dev_null_tty.api as api

    signed = {}
    monkeypatch.setattr(
        api,
        "signed_headers",
        lambda method, path, body, key_path: signed.update(
            method=method,
            path=path,
            body=body,
        )
        or {},
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def geturl(self) -> str:
            return "https://dev-null.sh/api/v1/notifications?cursor=next%2Fpage"

        def read(self) -> bytes:
            return json.dumps(
                {
                    "data": [
                        {
                            "id": "notification-id",
                            "type": "starred",
                            "subject_id": "commit-id",
                            "reason": None,
                            "read_at": None,
                            "created_at": "2026-10-20T18:00:00Z",
                            "actor": {
                                "username": "octocat",
                                "name": "The Octocat",
                                "avatar_url": None,
                                "bio": None,
                            },
                            "actors": [],
                            "group_id": "group-id",
                            "event_count": 2,
                            "actors_count": 2,
                            "unread_count": 1,
                            "commit": {
                                "id": "commit-id",
                                "body": "A commit that was starred.",
                                "created_at": "2026-10-20T17:00:00Z",
                                "stars_count": 2,
                                "replies_count": 0,
                                "author": {
                                    "username": "reader",
                                    "name": "Reader",
                                },
                            },
                        }
                    ],
                    "links": {"next": "https://dev-null.sh/api/v1/notifications?cursor=next"},
                    "meta": {"next_cursor": "older"},
                    "read_at": "2026-10-20T18:01:00Z",
                    "filters": {"type": "all"},
                }
            ).encode("utf-8")

    class Opener:
        request = None

        def open(self, request, timeout: int) -> Response:
            self.request = request
            return Response()

    client = SignedApiClient(tmp_path / "id_ed25519")
    opener = Opener()
    client._opener = opener

    page = client.get_notifications("next/page")

    assert signed == {
        "method": "GET",
        "path": "/api/v1/notifications?cursor=next%2Fpage",
        "body": b"",
    }
    assert page.next_cursor == "older"
    assert page.read_at == "2026-10-20T18:01:00Z"
    assert page.notifications == (
        ApiNotification(
            "notification-id",
            "starred",
            "2026-10-20T18:00:00Z",
            ApiUser("octocat", "The Octocat", None, None),
            2,
            2,
            1,
            None,
            ApiCommit(
                "commit-id",
                "reader",
                "Reader",
                "A commit that was starred.",
                "2026-10-20T17:00:00Z",
                2,
                0,
            ),
        ),
    )


def test_get_commit_detail_parses_ancestors_and_tombstones(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    signed = {}
    monkeypatch.setattr(
        api,
        "signed_headers",
        lambda method, path, body, key_path: signed.update(
            method=method,
            path=path,
            body=body,
        )
        or {},
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def geturl(self) -> str:
            return "https://dev-null.sh/api/v1/commits/reply-id"

        def read(self) -> bytes:
            return json.dumps(
                {
                    "data": {
                        "id": "reply-id",
                        "body": "The latest reply.",
                        "created_at": "2026-10-20T18:00:00Z",
                        "author": {"username": "octocat", "name": "The Octocat"},
                    },
                    "ancestors": [
                        {
                            "id": "root-id",
                            "body": "Original post.",
                            "created_at": "2026-10-20T16:00:00Z",
                            "author": {"username": "root", "name": "Root"},
                        },
                        {
                            "id": "deleted-id",
                            "parent_id": "root-id",
                            "deleted_at": "2026-10-20T17:00:00Z",
                            "replies_count": 1,
                        },
                    ],
                }
            ).encode("utf-8")

    class Opener:
        def open(self, request, timeout: int) -> Response:
            return Response()

    client = SignedApiClient(tmp_path / "id_ed25519")
    client._opener = Opener()

    detail = client.get_commit_detail("reply-id")

    assert signed == {
        "method": "GET",
        "path": "/api/v1/commits/reply-id",
        "body": b"",
    }
    assert detail.commit == ApiCommit(
        "reply-id",
        "octocat",
        "The Octocat",
        "The latest reply.",
        "2026-10-20T18:00:00Z",
        0,
        0,
    )
    assert detail.ancestors == (
        ApiCommitContext(
            "root-id",
            "root",
            "Root",
            "Original post.",
            "2026-10-20T16:00:00Z",
            0,
        ),
        ApiCommitContext(
            "deleted-id",
            None,
            None,
            None,
            None,
            1,
            deleted=True,
        ),
    )


def test_set_commit_star_posts_or_deletes_signed_request(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    signed = []
    monkeypatch.setattr(
        api,
        "signed_headers",
        lambda method, path, body, key_path: signed.append((method, path, body)) or {},
    )

    class Response:
        def __init__(self, request) -> None:
            self.request = request

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def geturl(self) -> str:
            return self.request.full_url

        def read(self) -> bytes:
            starred = self.request.get_method() == "POST"
            return json.dumps(
                {
                    "data": {
                        "id": "star-target",
                        "body": "A commit to star.",
                        "created_at": "2026-10-20T18:00:00Z",
                        "stars_count": int(starred),
                        "replies_count": 0,
                        "starred": starred,
                        "author": {"username": "octocat", "name": "The Octocat"},
                    }
                }
            ).encode("utf-8")

    class Opener:
        def open(self, request, timeout: int) -> Response:
            return Response(request)

    client = SignedApiClient(tmp_path / "id_ed25519")
    client._opener = Opener()

    starred_commit = client.set_commit_star("star-target", True)
    unstarred_commit = client.set_commit_star("star-target", False)

    assert signed == [
        ("POST", "/api/v1/commits/star-target/star", b""),
        ("DELETE", "/api/v1/commits/star-target/star", b""),
    ]
    assert starred_commit.starred is True
    assert starred_commit.stars_count == 1
    assert unstarred_commit.starred is False
    assert unstarred_commit.stars_count == 0


def test_create_commit_posts_signed_replies_and_quotes(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    signed = []
    monkeypatch.setattr(
        api,
        "signed_headers",
        lambda method, path, body, key_path: signed.append(
            {"method": method, "path": path, "body": body}
        )
        or {"X-SSH-Signature": "signature"},
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def geturl(self) -> str:
            return "https://dev-null.sh/api/v1/commits"

        def read(self) -> bytes:
            return json.dumps(
                {
                    "data": {
                        "id": "reply-id",
                        "body": "Thanks for sharing.",
                        "created_at": "2026-10-20T18:00:00Z",
                        "stars_count": 0,
                        "replies_count": 0,
                        "author": {"username": "octocat", "name": "The Octocat"},
                    }
                }
            ).encode("utf-8")

    class Opener:
        request = None

        def open(self, request, timeout: int) -> Response:
            self.request = request
            return Response()

    client = SignedApiClient(tmp_path / "id_ed25519")
    opener = Opener()
    client._opener = opener

    reply = client.create_commit("Thanks for sharing.", parent_id="parent-id")
    quote = client.create_commit(
        "Worth highlighting.",
        quoted_commit_id="quoted-id",
    )

    assert signed == [
        {
            "method": "POST",
            "path": "/api/v1/commits",
            "body": b'{"body":"Thanks for sharing.","parent_id":"parent-id"}',
        },
        {
            "method": "POST",
            "path": "/api/v1/commits",
            "body": b'{"body":"Worth highlighting.","quoted_commit_id":"quoted-id"}',
        },
    ]
    assert json.loads(signed[0]["body"]) == {
        "body": "Thanks for sharing.",
        "parent_id": "parent-id",
    }
    assert opener.request.get_header("Content-type") == "application/json"
    assert reply.id == quote.id == "reply-id"


def test_read_commit_images_validates_formats_and_size(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    image_paths = (
        tmp_path / "photo.jpg",
        tmp_path / "photo.png",
        tmp_path / "photo.webp",
    )
    image_paths[0].write_bytes(b"\xff\xd8\xffjpeg")
    image_paths[1].write_bytes(b"\x89PNG\r\n\x1a\npng")
    image_paths[2].write_bytes(b"RIFFxxxxWEBPwebp")

    images = SignedApiClient._read_commit_images(image_paths)

    assert [image[1:] for image in images] == [
        ("image/jpeg", "jpg"),
        ("image/png", "png"),
        ("image/webp", "webp"),
    ]

    invalid_image = tmp_path / "invalid.gif"
    invalid_image.write_bytes(b"GIF89a")
    with pytest.raises(ApiError, match="JPEG, PNG, or WebP"):
        SignedApiClient._read_commit_images((invalid_image,))

    oversized_image = tmp_path / "oversized.png"
    oversized_image.write_bytes(b"\x89PNG\r\n\x1a\nx")
    monkeypatch.setattr(api, "MAX_COMMIT_IMAGE_BYTES", 8)
    with pytest.raises(ApiError, match="larger than 10 MB"):
        SignedApiClient._read_commit_images((oversized_image,))


def test_fzf_image_picker_streams_supported_files_and_limits_selection(
    monkeypatch, tmp_path
) -> None:
    import dev_null_tty as dev_null_tty_module

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(
        dev_null_tty_module.Path,
        "home",
        classmethod(lambda cls: home),
    )
    monkeypatch.setenv("KITTY_WINDOW_ID", "7")
    monkeypatch.setattr(
        dev_null_tty_module.shutil,
        "which",
        lambda command: {
            "find": "/usr/bin/find",
            "fzf": "/usr/bin/fzf",
        }.get(command),
    )
    processes = []
    finder_outputs = []

    class FakeFinder:
        def __init__(self, arguments, **kwargs) -> None:
            self.arguments = arguments
            self.stdout = BytesIO()
            self.returncode = 0
            processes.append((self, kwargs))

        def wait(self) -> int:
            return self.returncode

        def poll(self) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

    monkeypatch.setattr(dev_null_tty_module.subprocess, "Popen", FakeFinder)
    selected_path = home / "photos" / "holiday.webp"

    def fake_run(arguments, **kwargs):
        processes.append((arguments, kwargs))
        finder_outputs.append(kwargs.get("stdin", kwargs.get("input")))
        return type(
            "FzfResult",
            (),
            {
                "returncode": 0,
                "stdout": os.fsencode(selected_path) + b"\0",
                "stderr": b"",
            },
        )()

    monkeypatch.setattr(dev_null_tty_module.subprocess, "run", fake_run)

    selected = dev_null_tty_module._pick_images_with_fzf(2)

    assert selected == (selected_path,)
    finder, finder_kwargs = processes[0]
    assert finder.arguments == [
        "/usr/bin/find",
        str(home),
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
    assert finder_kwargs["stdout"] == dev_null_tty_module.subprocess.PIPE
    assert finder_outputs == [finder.stdout]
    arguments, kwargs = processes[1]
    assert arguments == [
        "/usr/bin/fzf",
        "--read0",
        "--print0",
        "--multi=2",
        "--prompt=Images> ",
        "--layout=reverse",
        "--border",
        "--preview",
        (
            f"{dev_null_tty_module.shlex.quote(dev_null_tty_module.sys.executable)} "
            "-m dev_null_tty.image_preview --image-id "
            f"{dev_null_tty_module._IMAGE_PREVIEW_ID} {{}}"
        ),
        "--preview-window=right,50%,border-left",
    ]
    assert kwargs["stdin"] is finder.stdout
    assert "--height=80%" not in arguments

    removal_selection = dev_null_tty_module._pick_images_with_fzf(
        2,
        candidates=(selected_path,),
        prompt="Remove images> ",
    )

    assert removal_selection == (selected_path,)
    removal_arguments, removal_kwargs = processes[2]
    assert removal_arguments[:7] == [
        "/usr/bin/fzf",
        "--read0",
        "--print0",
        "--multi=1",
        "--prompt=Remove images> ",
        "--layout=reverse",
        "--border",
    ]
    assert removal_arguments[7:] == arguments[7:]
    assert removal_kwargs["input"] == os.fsencode(selected_path) + b"\0"


def test_fzf_picker_reports_missing_dependency(monkeypatch) -> None:
    import dev_null_tty as dev_null_tty_module

    monkeypatch.setattr(
        dev_null_tty_module.shutil,
        "which",
        lambda command: "/usr/bin/find" if command == "find" else None,
    )

    with pytest.raises(ApiError, match="Install fzf"):
        dev_null_tty_module._pick_images_with_fzf(1)


def test_create_commit_with_images_signs_and_sends_multipart(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    image_path = tmp_path / "photo.png"
    image_bytes = b"\x89PNG\r\n\x1a\nimage-data"
    image_path.write_bytes(image_bytes)
    client = SignedApiClient(tmp_path / "device-key")
    monkeypatch.setattr(
        client,
        "_lookup_cli_signing_key",
        lambda: api._CliSigningKey("ssh-ed25519 dGVzdC1rZXk", None),
    )
    monkeypatch.setattr(api.shutil, "which", lambda name: "/usr/bin/ssh-keygen")
    monkeypatch.setattr(api.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(api.secrets, "token_hex", lambda length: "a" * (length * 2))

    signed_payloads = []

    def fake_sign(args, **kwargs) -> None:
        payload_path = Path(args[-1])
        signed_payloads.append(payload_path.read_bytes())
        Path(f"{payload_path}.sig").write_text(
            "-----BEGIN SSH SIGNATURE-----\nc2ln\n-----END SSH SIGNATURE-----\n",
            encoding="ascii",
        )

    monkeypatch.setattr(api.subprocess, "run", fake_sign)
    requests = []

    def fake_cli_request(path, body, headers, *, timeout) -> bytes:
        requests.append((path, body, headers, timeout))
        return b"https://dev-null.sh/commits/created"

    monkeypatch.setattr(client, "_cli_request", fake_cli_request)

    result = client.create_commit_with_images(
        "A caption.",
        (image_path,),
        parent_id="reply-parent",
    )

    expected_payload = (
        f"1700000000\n{'a' * 32}\nreply-parent\n"
        f"{hashlib.sha256(image_bytes).hexdigest()}\nA caption."
    ).encode()
    assert result == "https://dev-null.sh/commits/created"
    assert signed_payloads == [expected_payload]
    path, multipart_body, headers, timeout = requests[0]
    assert path == "/api/cli/commits"
    assert timeout == 600
    assert headers["X-SSH-Signature"] == "c2ln"
    assert expected_payload in multipart_body
    assert b'name="images[]"; filename="image-1.png"' in multipart_body
    assert b"Content-Type: image/png" in multipart_body
    assert image_bytes in multipart_body


def test_cli_signing_key_lookup_uses_registered_ssh_agent_key(monkeypatch, tmp_path) -> None:
    import dev_null_tty.api as api

    public_key = "ssh-ed25519 dGVzdGtleQ=="
    monkeypatch.setattr(api.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(api.shutil, "which", lambda name: "/usr/bin/ssh-add")
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda *args, **kwargs: type(
            "AgentResult",
            (),
            {"returncode": 0, "stdout": f"{public_key} agent-comment\n"},
        )(),
    )
    client = SignedApiClient(tmp_path / "device-key")
    requests = []

    def fake_cli_request(path, body, headers, *, timeout) -> bytes:
        requests.append((path, body, headers, timeout))
        return f"{public_key} server-comment\n".encode()

    monkeypatch.setattr(client, "_cli_request", fake_cli_request)

    selected_key = client._lookup_cli_signing_key()

    assert selected_key == api._CliSigningKey(public_key, None)
    assert requests == [
        (
            "/api/cli/key-lookups",
            f"{public_key}\n".encode("ascii"),
            {
                "Accept": "text/plain",
                "Content-Type": "text/plain; charset=utf-8",
                "User-Agent": "dev-null-tty",
            },
            30,
        )
    ]


@pytest.mark.asyncio
async def test_logout_clears_local_session(monkeypatch) -> None:
    removed = []
    monkeypatch.setattr("dev_null_tty.remove_device_identity", lambda: removed.append(True))
    app = DevNullApp()

    async with app.run_test():
        app._set_authenticated_user(
            SignedApiClient.__new__(SignedApiClient),
            ApiUser("octocat", "The Octocat", None, "Building in public."),
        )
        await app._logout()

        assert removed == [True]
        assert app.authenticated_user is None
        assert app.api_client is None
        assert app.query_one("#sign-in").display
        assert not app.query_one("#logout").display
        assert "Signed out locally" in str(app.query_one("#sign-in-status", Static).render())
