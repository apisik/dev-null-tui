"""Per-user Linux URI handler integration."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from . import api
from .api import ApiError, authorization_callback_state

DESKTOP_ID = "dev-null-tui-callback.desktop"
MIME_TYPE = "x-scheme-handler/devnull"
HANDLER_MARKER = "X-Dev-Null-TUI-Handler=true"
MAX_CALLBACK_LENGTH = 8192


def callback_handoff_path(state: str) -> Path:
    state_digest = hashlib.sha256(state.encode("ascii")).hexdigest()
    return api.DEVICE_DIRECTORY / f"callback-{state_digest}.url"


def deliver_authorization_callback(callback_url: str) -> Path:
    """Queue a validated custom-scheme callback for the TUI process."""

    if len(callback_url) > MAX_CALLBACK_LENGTH:
        raise ApiError("The authorization callback URL is too long.")
    state, _ = authorization_callback_state(callback_url)
    api.ensure_device_directory()
    destination = callback_handoff_path(state)
    temporary_path = api.DEVICE_DIRECTORY / f".callback-{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as callback_file:
            callback_file.write(callback_url)
            callback_file.flush()
            os.fsync(callback_file.fileno())
        os.replace(temporary_path, destination)
    except OSError as error:
        temporary_path.unlink(missing_ok=True)
        raise ApiError("Could not deliver the browser callback to the running TUI.") from error
    return destination


def _quote_desktop_argument(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def _applications_directory() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share")).expanduser()
    if not data_home.is_absolute():
        raise ApiError("XDG_DATA_HOME must be an absolute path.")
    return data_home / "applications"


def register_uri_handler() -> Path:
    """Install this app as the per-user handler for devnull:// links."""

    if not sys.platform.startswith("linux"):
        raise ApiError("Automatic URI-handler registration is supported on Linux only.")
    xdg_mime = shutil.which("xdg-mime")
    if xdg_mime is None:
        raise ApiError("Install xdg-utils to register the devnull:// callback handler.")
    update_database = shutil.which("update-desktop-database")
    if update_database is None:
        raise ApiError("Install desktop-file-utils to refresh the devnull:// handler registry.")

    try:
        current = subprocess.run(
            [xdg_mime, "query", "default", MIME_TYPE],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ApiError("Could not check the current devnull:// handler.") from error
    if current and current != DESKTOP_ID:
        raise ApiError(f"{MIME_TYPE} is already assigned to {current}; it was left unchanged.")

    applications = _applications_directory()
    desktop_file = applications / DESKTOP_ID
    temporary_path: Path | None = None
    try:
        applications.mkdir(mode=0o755, parents=True, exist_ok=True)
        if applications.is_symlink() or not applications.is_dir():
            raise ApiError("The desktop applications path must be a real directory.")
        if desktop_file.is_symlink():
            raise ApiError("The callback desktop entry must not be a symbolic link.")
        if desktop_file.exists() and HANDLER_MARKER not in desktop_file.read_text(encoding="utf-8"):
            raise ApiError(f"Refusing to replace the unrelated desktop entry {desktop_file.name}.")

        contents = "\n".join(
            (
                "[Desktop Entry]",
                "Type=Application",
                "Name=dev-null-tty callback handler",
                "Terminal=false",
                f"Exec={_quote_desktop_argument(sys.executable)} -m dev_null_tty --callback %u",
                f"MimeType={MIME_TYPE};",
                "Categories=Utility;",
                HANDLER_MARKER,
                "",
            )
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=applications,
            prefix=".dev-null-tui-",
            delete=False,
        ) as desktop_temp:
            temporary_path = Path(desktop_temp.name)
            desktop_temp.write(contents)
        temporary_path.chmod(0o644)
        os.replace(temporary_path, desktop_file)
    except ApiError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ApiError("Could not install the devnull:// desktop handler.") from error

    try:
        subprocess.run(
            [update_database, str(applications)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        subprocess.run(
            [xdg_mime, "default", DESKTOP_ID, MIME_TYPE],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ApiError(
            "The handler file was installed, but xdg-mime could not set it as the default."
        ) from error
    return desktop_file
