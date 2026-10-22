"""Signed requests to the /dev/null mobile API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import platform
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence
from urllib.parse import parse_qs, quote, urlencode, urlsplit

API_ORIGIN = "https://dev-null.sh"
SIGNATURE_NAMESPACE = "devnull-api"
DEVICE_DIRECTORY = Path.home() / ".dev-null-tui"
DEVICE_NAME = f"dev-null-tty on {platform.system()}"
REDIRECT_URI = "devnull://auth/callback"
LogName = Literal["following", "latest", "trending"]
MAX_COMMIT_IMAGES = 3
MAX_COMMIT_IMAGE_BYTES = 10_240_000


@dataclass(frozen=True)
class ApiUser:
    username: str
    name: str | None
    avatar_url: str | None
    bio: str | None


@dataclass(frozen=True)
class ApiCommitPreview:
    id: str | None
    username: str | None
    author_name: str | None
    body: str | None
    deleted: bool = False


@dataclass(frozen=True)
class ApiCommit:
    id: str
    username: str
    author_name: str | None
    body: str
    created_at: str
    stars_count: int
    replies_count: int
    starred: bool | None = None
    images: tuple[ApiCommitImage, ...] = ()
    quoted_commit_id: str | None = None
    quoted_commit: ApiCommitPreview | None = None


@dataclass(frozen=True)
class ApiCommitImage:
    id: str
    url: str
    width: int
    height: int
    description: str | None


@dataclass(frozen=True)
class ApiCommitContext:
    id: str
    username: str | None
    author_name: str | None
    body: str | None
    created_at: str | None
    replies_count: int
    deleted: bool = False


@dataclass(frozen=True)
class ApiCommitDetail:
    commit: ApiCommit
    ancestors: tuple[ApiCommitContext, ...]


@dataclass(frozen=True)
class _CliSigningKey:
    key_id: str
    private_key_path: Path | None


@dataclass(frozen=True)
class ApiLog:
    commits: tuple[ApiCommit, ...]
    next_cursor: str | None
    is_ranked: bool
    new_commits_count: int
    suggestions: tuple[ApiUser, ...]


@dataclass(frozen=True)
class ApiNotification:
    id: str
    type: str
    created_at: str
    actor: ApiUser | None
    actors_count: int
    event_count: int
    unread_count: int
    reason: str | None
    commit: ApiCommit | None


@dataclass(frozen=True)
class ApiNotifications:
    notifications: tuple[ApiNotification, ...]
    next_cursor: str | None
    read_at: str | None


@dataclass(frozen=True)
class DeviceAuthorization:
    key_path: Path
    state: str
    code_verifier: str
    authorization_url: str


class ApiError(Exception):
    """An expected error returned by the signed API."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _load_existing_device_keypair() -> Path | None:
    private_key = DEVICE_DIRECTORY / "id_ed25519"
    public_key = DEVICE_DIRECTORY / "id_ed25519.pub"
    if private_key.is_symlink() or public_key.is_symlink():
        raise ApiError("The device keypair must not use symbolic links.")
    private_exists = private_key.is_file()
    public_exists = public_key.is_file()
    if private_key.exists() != private_exists or public_key.exists() != public_exists:
        raise ApiError("The device-key files must be regular files.")
    if private_exists != public_exists:
        raise ApiError("The saved device keypair is incomplete; repair or remove it before signing in.")
    if not private_exists:
        return None
    try:
        private_key.chmod(0o600)
        public_key.chmod(0o644)
        _device_fingerprint(private_key)
    except ApiError:
        raise
    except OSError as error:
        raise ApiError("Could not secure the app's existing device keypair.") from error
    return private_key


def existing_device_identity() -> Path | None:
    """Return the saved app key if one exists, without creating a new identity."""

    if DEVICE_DIRECTORY.is_symlink():
        raise ApiError("The app-data directory must not be a symbolic link.")
    if not DEVICE_DIRECTORY.exists():
        return None
    ensure_device_directory()
    return _load_existing_device_keypair()


def remove_device_identity() -> None:
    """Remove only the app's local SSH keypair, leaving other app data untouched."""

    if DEVICE_DIRECTORY.is_symlink():
        raise ApiError("The app-data directory must not be a symbolic link.")
    if not DEVICE_DIRECTORY.exists():
        return
    ensure_device_directory()
    private_key = DEVICE_DIRECTORY / "id_ed25519"
    public_key = DEVICE_DIRECTORY / "id_ed25519.pub"
    for key_file in (private_key, public_key):
        if key_file.is_symlink() or (key_file.exists() and not key_file.is_file()):
            raise ApiError("The device-key files must be regular files before logging out.")
    try:
        public_key.unlink(missing_ok=True)
        private_key.unlink(missing_ok=True)
    except OSError as error:
        raise ApiError("Could not remove the app's local device keypair.") from error


def resolve_ssh_identity() -> Path:
    """Create or return this app's dedicated Ed25519 device keypair."""

    ensure_device_directory()
    existing_key = _load_existing_device_keypair()
    if existing_key is not None:
        return existing_key

    private_key = DEVICE_DIRECTORY / "id_ed25519"
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        raise ApiError("ssh-keygen is required to create the app's device key.")
    try:
        subprocess.run(
            [
                ssh_keygen,
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "dev-null-tty",
                "-f",
                str(private_key),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        private_key.chmod(0o600)
        private_key.with_suffix(".pub").chmod(0o644)
        _device_fingerprint(private_key)
    except ApiError:
        raise
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ApiError("Could not create or secure the app's device keypair.") from error
    return private_key


def ensure_device_directory() -> None:
    """Create the private app-data directory and enforce owner-only access."""

    try:
        DEVICE_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
        if DEVICE_DIRECTORY.is_symlink() or not DEVICE_DIRECTORY.is_dir():
            raise ApiError("The app-data directory must be a real directory.")
        DEVICE_DIRECTORY.chmod(0o700)
    except ApiError:
        raise
    except OSError as error:
        raise ApiError("Could not create or secure ~/.dev-null-tui.") from error


def _device_fingerprint(key_path: Path) -> str:
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        raise ApiError("ssh-keygen is required to identify the device key.")
    try:
        result = subprocess.run(
            [ssh_keygen, "-lf", f"{key_path}.pub"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ApiError("Could not read the app's device-key fingerprint.") from error
    parts = result.stdout.split()
    if len(parts) < 3 or not parts[1].startswith("SHA256:") or parts[-1] != "(ED25519)":
        raise ApiError("The app's saved device key is not a valid Ed25519 key.")
    return parts[1]


def create_device_authorization() -> DeviceAuthorization:
    """Create an OAuth-style authorization URL and an in-memory PKCE verifier."""

    key_path = resolve_ssh_identity()
    fingerprint = _device_fingerprint(key_path)
    code_verifier = secrets.token_urlsafe(32)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)
    query = urlencode(
        {
            "fingerprint": fingerprint,
            "device_name": DEVICE_NAME,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "redirect_uri": REDIRECT_URI,
        }
    )
    return DeviceAuthorization(
        key_path=key_path,
        state=state,
        code_verifier=code_verifier,
        authorization_url=f"{API_ORIGIN}/mobile/authorize?{query}",
    )


def authorization_callback_state(callback_url: str) -> tuple[str, dict[str, list[str]]]:
    """Validate the custom-scheme callback's shape and return its state and query."""
    try:
        callback = urlsplit(callback_url.strip())
        query = parse_qs(callback.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise ApiError("The authorization callback URL is malformed.") from error
    if callback.scheme != "devnull" or callback.netloc != "auth" or callback.path != "/callback" or callback.fragment:
        raise ApiError("Paste the full devnull://auth/callback URL from the browser.")
    states = query.get("state", [])
    if len(states) != 1 or not states[0].isascii():
        raise ApiError("The authorization callback must contain exactly one valid state.")
    return states[0], query


def authorization_code_from_callback(callback_url: str, expected_state: str) -> str:
    """Validate the custom-scheme callback and return its one-time code."""

    state, query = authorization_callback_state(callback_url)
    if not hmac.compare_digest(state, expected_state):
        raise ApiError("The authorization state did not match. Start sign-in again.")
    errors = query.get("error", [])
    if errors:
        if len(errors) != 1:
            raise ApiError("The authorization callback is malformed.")
        if errors[0] == "access_denied":
            raise ApiError("Sign-in was canceled in the browser.")
        raise ApiError(f"Authorization failed: {errors[0]}")
    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise ApiError("The authorization callback did not contain one code.")
    return codes[0]


def _sign(message: bytes, key_path: Path) -> str:
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        raise ApiError("ssh-keygen is required to sign /dev/null API requests.")

    with tempfile.TemporaryDirectory(prefix="dev-null-signature-") as temporary_directory:
        payload_path = Path(temporary_directory) / "request"
        payload_path.write_bytes(message)
        payload_path.chmod(0o600)
        command = [
            ssh_keygen,
            "-Y",
            "sign",
            "-n",
            SIGNATURE_NAMESPACE,
            "-f",
            str(key_path),
            str(payload_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=20)
        except subprocess.CalledProcessError as error:
            if b"incorrect passphrase" in error.stderr.lower() or b"passphrase" in error.stderr.lower():
                raise ApiError(
                    "The SSH key requires a passphrase. Unlock it with ssh-agent and retry."
                ) from error
            raise ApiError("ssh-keygen could not sign the API request.") from error
        except subprocess.TimeoutExpired as error:
            raise ApiError("ssh-keygen timed out while signing the API request.") from error

        signature_path = payload_path.with_name(f"{payload_path.name}.sig")
        try:
            armored_signature = signature_path.read_text(encoding="ascii")
        except OSError as error:
            raise ApiError("ssh-keygen did not produce a signature.") from error
        signature = "".join(
            line.strip()
            for line in armored_signature.splitlines()
            if not line.startswith("-----")
        )
        if not signature:
            raise ApiError("ssh-keygen produced an empty signature.")
        return signature


def signed_headers(method: str, path_and_query: str, body: bytes, key_path: Path) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    message = b"\n".join(
        (timestamp.encode(), nonce.encode(), method.upper().encode(), path_and_query.encode(), body)
    )
    return {
        "X-Devnull-Timestamp": timestamp,
        "X-Devnull-Nonce": nonce,
        "X-SSH-Signature": _sign(message, key_path),
    }


class SignedApiClient:
    """A minimal client for signed /dev/null API calls."""

    def __init__(self, key_path: Path, origin: str = API_ORIGIN) -> None:
        parsed_origin = urlsplit(origin)
        if parsed_origin.scheme != "https" or not parsed_origin.netloc or parsed_origin.path:
            raise ValueError("The API origin must be an HTTPS origin.")
        self.key_path = key_path
        self.origin = origin.rstrip("/")
        self._opener = urllib.request.build_opener(_NoRedirect)

    def get_current_user(self) -> ApiUser:
        payload = self._request("GET", "/api/v1/me")
        return self._user_from_payload(payload)

    def search_users(self, query: str) -> tuple[ApiUser, ...]:
        path = f"/search/users?{urlencode({'q': query})}"
        request = urllib.request.Request(
            f"{self.origin}{path}",
            headers={
                "Accept": "application/json",
                "User-Agent": "dev-null-tty",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=20) as response:
                response_origin = urlsplit(response.geturl())
                origin = urlsplit(self.origin)
                if (
                    response_origin.scheme != "https"
                    or response_origin.netloc.casefold() != origin.netloc.casefold()
                ):
                    raise ApiError("The user-search response came from an unexpected origin.")
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise ApiError(f"User search returned HTTP {error.code}.") from error
        except urllib.error.URLError as error:
            raise ApiError(f"Could not reach {self.origin} for user search: {error.reason}") from error
        except json.JSONDecodeError as error:
            raise ApiError("User search returned invalid JSON.") from error
        if not isinstance(payload, dict):
            raise ApiError("User search returned an unexpected response.")
        try:
            users = payload["users"]
            if not isinstance(users, list):
                raise TypeError
            return tuple(self._search_user_from_payload(user) for user in users)
        except (KeyError, TypeError) as error:
            raise ApiError("User search returned an unexpected response.") from error

    def get_log(self, log_name: LogName, cursor: str | None = None) -> ApiLog:
        if log_name not in {"following", "latest", "trending"}:
            raise ValueError(f"Unsupported log: {log_name}")
        path = f"/api/v1/log/{log_name}"
        if cursor is not None:
            path = f"{path}?{urlencode({'cursor': cursor})}"
        payload = self._request("GET", path)
        return self._log_from_payload(payload)

    def get_replies(self, commit_id: str, cursor: str | None = None) -> ApiLog:
        path = f"/api/v1/commits/{quote(commit_id, safe='')}/replies"
        query = {"newest": "1"}
        if cursor is not None:
            query["cursor"] = cursor
        payload = self._request("GET", f"{path}?{urlencode(query)}")
        return self._log_from_payload(payload)

    def get_commit_detail(self, commit_id: str) -> ApiCommitDetail:
        path = f"/api/v1/commits/{quote(commit_id, safe='')}"
        payload = self._request("GET", path)
        try:
            commit_payload = payload["data"]
            ancestors_payload = payload["ancestors"]
            if not isinstance(ancestors_payload, list):
                raise TypeError
            commit = self._commit_from_payload(commit_payload)
            ancestors = tuple(
                self._commit_context_from_payload(ancestor)
                for ancestor in ancestors_payload
            )
            return ApiCommitDetail(commit, ancestors)
        except (KeyError, TypeError, ValueError) as error:
            raise ApiError("The API returned an unexpected commit-detail response.") from error

    def get_commit_image(self, image: ApiCommitImage) -> bytes:
        image_url = urlsplit(image.url)
        api_origin = urlsplit(self.origin)
        if (
            image_url.scheme != "https"
            or image_url.netloc.casefold() != api_origin.netloc.casefold()
            or image_url.username is not None
            or image_url.password is not None
            or not image_url.path.startswith("/")
        ):
            raise ApiError("The API returned an image URL from an unexpected origin.")

        request = urllib.request.Request(
            image.url,
            headers={
                "Accept": "image/*",
                "User-Agent": "dev-null-tty",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=20) as response:
                response_origin = urlsplit(response.geturl())
                if (
                    response_origin.scheme != "https"
                    or response_origin.netloc.casefold() != api_origin.netloc.casefold()
                ):
                    raise ApiError("The image response came from an unexpected origin.")
                if not response.headers.get_content_type().startswith("image/"):
                    raise ApiError("The API returned a non-image response for a commit image.")
                contents = response.read(MAX_COMMIT_IMAGE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise ApiError(f"The API returned HTTP {error.code} while loading a commit image.") from error
        except urllib.error.URLError as error:
            raise ApiError(f"Could not reach {self.origin} to load a commit image: {error.reason}") from error
        if len(contents) > MAX_COMMIT_IMAGE_BYTES:
            raise ApiError("The commit image exceeds the 10 MB download limit.")
        return contents

    def get_notifications(self, cursor: str | None = None) -> ApiNotifications:
        path = "/api/v1/notifications"
        if cursor is not None:
            path = f"{path}?{urlencode({'cursor': cursor})}"
        payload = self._request("GET", path)
        return self._notifications_from_payload(payload)

    def set_commit_star(self, commit_id: str, starred: bool) -> ApiCommit:
        path = f"/api/v1/commits/{quote(commit_id, safe='')}/star"
        payload = self._request("POST" if starred else "DELETE", path)
        try:
            commit_payload = payload["data"]
        except (KeyError, TypeError) as error:
            raise ApiError("The API returned an unexpected starred-commit response.") from error
        commit = self._commit_from_payload(commit_payload)
        if commit.starred is None:
            raise ApiError("The API did not return the updated star state.")
        return commit

    def create_commit(
        self,
        body: str,
        parent_id: str | None = None,
        quoted_commit_id: str | None = None,
    ) -> ApiCommit:
        if parent_id is not None and quoted_commit_id is not None:
            raise ValueError("A commit cannot be both a reply and a quote.")
        data = {"body": body}
        if parent_id is not None:
            data["parent_id"] = parent_id
        if quoted_commit_id is not None:
            data["quoted_commit_id"] = quoted_commit_id
        request_body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        payload = self._request("POST", "/api/v1/commits", request_body)
        try:
            commit = payload["data"]
        except KeyError as error:
            raise ApiError("The API returned an unexpected created-commit response.") from error
        return self._commit_from_payload(commit)

    def create_commit_with_images(
        self,
        body: str,
        image_paths: Sequence[Path],
        parent_id: str | None = None,
    ) -> str:
        if not body.strip():
            raise ApiError("A commit message is required when attaching images.")
        if not image_paths or len(image_paths) > MAX_COMMIT_IMAGES:
            raise ApiError(f"A commit can carry between 1 and {MAX_COMMIT_IMAGES} images.")

        images = self._read_commit_images(image_paths)
        signing_key = self._lookup_cli_signing_key()
        image_hashes = ",".join(
            hashlib.sha256(contents).hexdigest()
            for contents, _content_type, _extension in images
        )
        signed_payload = (
            f"{int(time.time())}\n"
            f"{secrets.token_hex(16)}\n"
            f"{parent_id or ''}\n"
            f"{image_hashes}\n"
            f"{body}"
        ).encode("utf-8")

        ssh_keygen = shutil.which("ssh-keygen")
        if ssh_keygen is None:
            raise ApiError("ssh-keygen is required to sign image commits.")

        try:
            with tempfile.TemporaryDirectory(prefix="dev-null-image-commit-") as directory:
                temporary_directory = Path(directory)
                payload_path = temporary_directory / "commit"
                payload_path.write_bytes(signed_payload)
                payload_path.chmod(0o600)

                signing_path = signing_key.private_key_path
                if signing_path is None:
                    signing_path = temporary_directory / "key.pub"
                    signing_path.write_text(f"{signing_key.key_id}\n", encoding="utf-8")
                    signing_path.chmod(0o600)

                subprocess.run(
                    [
                        ssh_keygen,
                        "-Y",
                        "sign",
                        "-n",
                        "devnull-commit-images",
                        "-f",
                        str(signing_path),
                        str(payload_path),
                    ],
                    check=True,
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    timeout=30,
                )
                signature_path = Path(f"{payload_path}.sig")
                signature_lines = signature_path.read_text(encoding="ascii").splitlines()
        except subprocess.CalledProcessError as error:
            detail = error.stderr.decode("utf-8", errors="replace").strip()
            message = f"Could not sign the image commit with the selected SSH key."
            if detail:
                message = f"{message} {detail}"
            raise ApiError(message) from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ApiError("Could not sign the image commit with the selected SSH key.") from error

        signature = "".join(
            line for line in signature_lines if not line.startswith("-----")
        )
        try:
            base64.b64decode(signature, validate=True)
        except (ValueError, TypeError) as error:
            raise ApiError("ssh-keygen produced an invalid image-commit signature.") from error

        body_bytes, boundary = self._multipart_commit_body(signed_payload, images)
        response = self._cli_request(
            "/api/cli/commits",
            body_bytes,
            {
                "Accept": "text/plain",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "dev-null-tty",
                "X-SSH-Signature": signature,
            },
            timeout=600,
        )
        try:
            commit_url = response.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise ApiError("The API returned an invalid image-commit response.") from error
        if not commit_url:
            raise ApiError("The API returned an empty image-commit response.")
        return commit_url

    def _lookup_cli_signing_key(self) -> _CliSigningKey:
        candidates: dict[str, _CliSigningKey] = {}
        ssh_add = shutil.which("ssh-add")
        if ssh_add is not None:
            try:
                agent_result = subprocess.run(
                    [ssh_add, "-L"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except subprocess.TimeoutExpired as error:
                raise ApiError("Could not read public keys from ssh-agent.") from error
            except OSError as error:
                raise ApiError("Could not read public keys from ssh-agent.") from error
            if agent_result.returncode == 0:
                for line in agent_result.stdout.splitlines():
                    key_id = self._ssh_key_id(line)
                    if key_id is not None:
                        candidates.setdefault(key_id, _CliSigningKey(key_id, None))

        ssh_directory = Path.home() / ".ssh"
        try:
            public_key_paths = sorted(ssh_directory.glob("*.pub"))
        except OSError as error:
            raise ApiError("Could not list SSH public keys in ~/.ssh.") from error
        for public_key_path in public_key_paths:
            private_key_path = public_key_path.with_suffix("")
            if not public_key_path.is_file() or not private_key_path.is_file():
                continue
            try:
                public_key = public_key_path.read_text(encoding="utf-8").splitlines()[0]
            except (IndexError, OSError, UnicodeError) as error:
                raise ApiError(f"Could not read SSH public key {public_key_path.name}.") from error
            key_id = self._ssh_key_id(public_key)
            if key_id is not None:
                candidates.setdefault(key_id, _CliSigningKey(key_id, private_key_path))

        if not candidates:
            raise ApiError("No Ed25519 or RSA SSH key found in ssh-agent or ~/.ssh.")

        selected_candidates = list(candidates.values())[:20]
        request_body = "".join(f"{candidate.key_id}\n" for candidate in selected_candidates)
        response = self._cli_request(
            "/api/cli/key-lookups",
            request_body.encode("ascii"),
            {
                "Accept": "text/plain",
                "Content-Type": "text/plain; charset=utf-8",
                "User-Agent": "dev-null-tty",
            },
            timeout=30,
        )
        try:
            public_key = response.decode("utf-8").splitlines()[0]
        except (IndexError, UnicodeDecodeError) as error:
            raise ApiError("The SSH key lookup returned an unexpected response.") from error
        selected_key_id = self._ssh_key_id(public_key)
        if selected_key_id is None:
            raise ApiError("The SSH key lookup returned an invalid public key.")
        for candidate in selected_candidates:
            if candidate.key_id == selected_key_id:
                return candidate
        raise ApiError("The SSH key lookup returned a key not sent by this app.")

    @staticmethod
    def _ssh_key_id(public_key: str) -> str | None:
        parts = public_key.split()
        if len(parts) < 2 or parts[0] not in {"ssh-ed25519", "ssh-rsa"}:
            return None
        try:
            base64.b64decode(parts[1], validate=True)
        except ValueError:
            return None
        return f"{parts[0]} {parts[1]}"

    @staticmethod
    def _read_commit_images(
        image_paths: Sequence[Path],
    ) -> list[tuple[bytes, str, str]]:
        images: list[tuple[bytes, str, str]] = []
        for image_path in image_paths:
            path = image_path.expanduser()
            try:
                if not path.is_file():
                    raise ApiError(f"Image file does not exist: {path}")
                if path.stat().st_size > MAX_COMMIT_IMAGE_BYTES:
                    raise ApiError(f"Image is larger than 10 MB: {path}")
                with path.open("rb") as image_file:
                    contents = image_file.read(MAX_COMMIT_IMAGE_BYTES + 1)
            except ApiError:
                raise
            except OSError as error:
                raise ApiError(f"Could not read image file {path}: {error}") from error

            if len(contents) > MAX_COMMIT_IMAGE_BYTES:
                raise ApiError(f"Image is larger than 10 MB: {path}")
            if contents.startswith(b"\xff\xd8\xff"):
                content_type, extension = "image/jpeg", "jpg"
            elif contents.startswith(b"\x89PNG\r\n\x1a\n"):
                content_type, extension = "image/png", "png"
            elif contents.startswith(b"RIFF") and contents[8:12] == b"WEBP":
                content_type, extension = "image/webp", "webp"
            else:
                raise ApiError(f"Image must be a JPEG, PNG, or WebP file: {path}")
            images.append((contents, content_type, extension))
        return images

    @staticmethod
    def _multipart_commit_body(
        payload: bytes,
        images: Sequence[tuple[bytes, str, str]],
    ) -> tuple[bytes, str]:
        boundary = f"dev-null-tty-{secrets.token_hex(16)}"
        parts = bytearray()

        def add_file(field_name: str, filename: str, content_type: str, contents: bytes) -> None:
            parts.extend(f"--{boundary}\r\n".encode("ascii"))
            parts.extend(
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode("ascii")
            )
            parts.extend(contents)
            parts.extend(b"\r\n")

        add_file("payload", "commit", "application/octet-stream", payload)
        for index, (contents, content_type, extension) in enumerate(images, start=1):
            add_file(
                "images[]",
                f"image-{index}.{extension}",
                content_type,
                contents,
            )
        parts.extend(f"--{boundary}--\r\n".encode("ascii"))
        return bytes(parts), boundary

    def _cli_request(
        self,
        path: str,
        body: bytes,
        headers: dict[str, str],
        *,
        timeout: int,
    ) -> bytes:
        request = urllib.request.Request(
            f"{self.origin}{path}",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                response_origin = urlsplit(response.geturl())
                origin = urlsplit(self.origin)
                if (
                    response_origin.scheme != "https"
                    or response_origin.netloc != origin.netloc
                ):
                    raise ApiError("The CLI API response came from an unexpected origin.")
                return response.read()
        except urllib.error.HTTPError as error:
            response_body = error.read().decode("utf-8", errors="replace").strip()
            raise ApiError(response_body or f"The CLI API returned HTTP {error.code}.") from error
        except urllib.error.URLError as error:
            raise ApiError(f"Could not reach {self.origin}: {error.reason}") from error

    def redeem_device(self, code: str, code_verifier: str) -> ApiUser:
        body = json.dumps(
            {"code": code, "code_verifier": code_verifier},
            separators=(",", ":"),
        ).encode("utf-8")
        payload = self._request("POST", "/api/v1/devices", body)
        return self._user_from_payload(payload)

    @staticmethod
    def _user_from_payload(payload: dict) -> ApiUser:
        try:
            data = payload["data"]
            return ApiUser(
                username=data["username"],
                name=data["name"],
                avatar_url=data.get("avatar_url"),
                bio=data.get("bio"),
            )
        except (KeyError, TypeError) as error:
            raise ApiError("The API returned an unexpected current-user response.") from error

    @staticmethod
    def _log_from_payload(payload: dict) -> ApiLog:
        try:
            entries = payload["data"]
            if not isinstance(entries, list):
                raise TypeError
            meta = payload.get("meta", {})
            if not isinstance(meta, dict):
                raise TypeError
            commits = tuple(SignedApiClient._commit_from_payload(entry) for entry in entries)
            next_cursor = meta.get("next_cursor", payload.get("next_cursor"))
            is_ranked = meta.get("is_ranked", payload.get("is_ranked", False))
            new_commits_count = meta.get(
                "new_commits_count", payload.get("new_commits_count", 0)
            )
            suggestions_data = meta.get("suggestions", payload.get("suggestions", []))
            if not isinstance(suggestions_data, list):
                raise TypeError
            suggestions = tuple(
                SignedApiClient._suggestion_from_payload(suggestion)
                for suggestion in suggestions_data
            )
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise TypeError
            if not isinstance(is_ranked, bool):
                raise TypeError
            if not isinstance(new_commits_count, int) or isinstance(new_commits_count, bool):
                raise TypeError
            if new_commits_count < 0:
                raise ValueError
            return ApiLog(
                commits=commits,
                next_cursor=next_cursor,
                is_ranked=is_ranked,
                new_commits_count=new_commits_count,
                suggestions=suggestions,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ApiError("The API returned an unexpected log response.") from error

    @staticmethod
    def _notifications_from_payload(payload: dict) -> ApiNotifications:
        try:
            entries = payload["data"]
            meta = payload.get("meta", {})
            if not isinstance(entries, list) or not isinstance(meta, dict):
                raise TypeError
            notifications = tuple(
                SignedApiClient._notification_from_payload(entry) for entry in entries
            )
            next_cursor = meta.get("next_cursor")
            read_at = payload.get("read_at")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise TypeError
            if read_at is not None and not isinstance(read_at, str):
                raise TypeError
            return ApiNotifications(notifications, next_cursor, read_at)
        except (KeyError, TypeError, ValueError) as error:
            raise ApiError("The API returned an unexpected notifications response.") from error

    @staticmethod
    def _notification_from_payload(payload: dict) -> ApiNotification:
        try:
            if not isinstance(payload, dict):
                raise TypeError
            actor_payload = payload.get("actor")
            actor = (
                SignedApiClient._suggestion_from_payload(actor_payload)
                if actor_payload is not None
                else None
            )
            commit_payload = payload.get("commit")
            commit = None
            if commit_payload is not None:
                if not isinstance(commit_payload, dict):
                    raise TypeError
                if commit_payload.get("deleted_at") is None:
                    commit = SignedApiClient._commit_from_payload(
                        commit_payload,
                        error_message="The API returned a malformed notification commit.",
                    )
            notification = ApiNotification(
                id=payload["id"],
                type=payload["type"],
                created_at=payload["created_at"],
                actor=actor,
                actors_count=payload["actors_count"],
                event_count=payload["event_count"],
                unread_count=payload["unread_count"],
                reason=payload.get("reason"),
                commit=commit,
            )
            if not all(
                isinstance(value, str)
                for value in (notification.id, notification.type, notification.created_at)
            ):
                raise TypeError
            if notification.reason is not None and not isinstance(notification.reason, str):
                raise TypeError
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (
                    notification.actors_count,
                    notification.event_count,
                    notification.unread_count,
                )
            ):
                raise TypeError
            return notification
        except (KeyError, TypeError, ValueError) as error:
            raise ApiError("The API returned a malformed notification.") from error

    @staticmethod
    def _commit_preview_from_payload(
        payload: dict,
        *,
        fallback_id: str | None = None,
    ) -> ApiCommitPreview:
        if not isinstance(payload, dict):
            raise TypeError
        preview_id = payload.get("id", fallback_id)
        author_payload = payload.get("author")
        if author_payload is not None and not isinstance(author_payload, dict):
            raise TypeError
        username = (
            author_payload.get("username")
            if author_payload is not None
            else payload.get("username")
        )
        author_name = (
            author_payload.get("name")
            if author_payload is not None
            else payload.get("author_name")
        )
        body = payload.get("body")
        deleted_at = payload.get("deleted_at")
        deleted = payload.get("deleted", False)
        if (
            (preview_id is not None and not isinstance(preview_id, str))
            or (username is not None and not isinstance(username, str))
            or (author_name is not None and not isinstance(author_name, str))
            or (body is not None and not isinstance(body, str))
            or not isinstance(deleted, bool)
        ):
            raise TypeError
        return ApiCommitPreview(
            id=preview_id,
            username=username,
            author_name=author_name,
            body=body,
            deleted=deleted or deleted_at is not None,
        )

    @staticmethod
    def _commit_from_payload(
        payload: dict,
        *,
        error_message: str = "The API returned a malformed commit in a log.",
    ) -> ApiCommit:
        try:
            if not isinstance(payload, dict):
                raise TypeError
            author = payload["author"]
            quoted_commit_id = payload.get("quoted_commit_id")
            if quoted_commit_id is not None and not isinstance(quoted_commit_id, str):
                raise TypeError
            quoted_payload = payload.get("quoted_commit")
            quoted_commit = (
                SignedApiClient._commit_preview_from_payload(
                    quoted_payload,
                    fallback_id=quoted_commit_id,
                )
                if quoted_payload is not None
                else None
            )
            if quoted_commit_id is None and quoted_commit is not None:
                quoted_commit_id = quoted_commit.id
            commit = ApiCommit(
                id=payload["id"],
                username=author["username"],
                author_name=author.get("name"),
                body=payload["body"],
                created_at=payload["created_at"],
                stars_count=payload.get("stars_count", 0),
                replies_count=payload.get("replies_count", 0),
                starred=payload.get("starred"),
                images=SignedApiClient._commit_images_from_payload(payload),
                quoted_commit_id=quoted_commit_id,
                quoted_commit=quoted_commit,
            )
            if not all(
                isinstance(value, str)
                for value in (
                    commit.id,
                    commit.username,
                    commit.body,
                    commit.created_at,
                )
            ):
                raise TypeError
            if commit.author_name is not None and not isinstance(commit.author_name, str):
                raise TypeError
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (commit.stars_count, commit.replies_count)
            ):
                raise TypeError
            if commit.starred is not None and not isinstance(commit.starred, bool):
                raise TypeError
            if not isinstance(author, dict):
                raise TypeError
            return commit
        except (KeyError, TypeError, ValueError) as error:
            raise ApiError(error_message) from error

    @staticmethod
    def _commit_images_from_payload(payload: dict) -> tuple[ApiCommitImage, ...]:
        images_payload = payload.get("images", [])
        if not isinstance(images_payload, list):
            raise TypeError
        images: list[ApiCommitImage] = []
        for image_payload in images_payload:
            if not isinstance(image_payload, dict):
                raise TypeError
            image = ApiCommitImage(
                id=image_payload["id"],
                url=image_payload["url"],
                width=image_payload["width"],
                height=image_payload["height"],
                description=image_payload.get("description"),
            )
            if not isinstance(image.id, str) or not isinstance(image.url, str):
                raise TypeError
            if (
                not isinstance(image.width, int)
                or isinstance(image.width, bool)
                or image.width <= 0
                or not isinstance(image.height, int)
                or isinstance(image.height, bool)
                or image.height <= 0
            ):
                raise TypeError
            if image.description is not None and not isinstance(image.description, str):
                raise TypeError
            images.append(image)
        return tuple(images)

    @staticmethod
    def _commit_context_from_payload(payload: dict) -> ApiCommitContext:
        try:
            if not isinstance(payload, dict):
                raise TypeError
            commit_id = payload["id"]
            if not isinstance(commit_id, str):
                raise TypeError
            deleted = payload.get("deleted_at") is not None
            replies_count = payload.get("replies_count", 0)
            if (
                not isinstance(replies_count, int)
                or isinstance(replies_count, bool)
                or replies_count < 0
            ):
                raise TypeError
            if deleted:
                return ApiCommitContext(
                    id=commit_id,
                    username=None,
                    author_name=None,
                    body=None,
                    created_at=None,
                    replies_count=replies_count,
                    deleted=True,
                )
            commit = SignedApiClient._commit_from_payload(payload)
            return ApiCommitContext(
                id=commit.id,
                username=commit.username,
                author_name=commit.author_name,
                body=commit.body,
                created_at=commit.created_at,
                replies_count=commit.replies_count,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ApiError("The API returned a malformed commit ancestor.") from error

    @staticmethod
    def _suggestion_from_payload(payload: dict) -> ApiUser:
        try:
            user = ApiUser(
                username=payload["username"],
                name=payload.get("name"),
                avatar_url=payload.get("avatar_url"),
                bio=payload.get("bio"),
            )
            if not isinstance(user.username, str) or any(
                value is not None and not isinstance(value, str)
                for value in (user.name, user.avatar_url, user.bio)
            ):
                raise TypeError
            return user
        except (KeyError, TypeError) as error:
            raise ApiError("The API returned a malformed log suggestion.") from error

    @staticmethod
    def _search_user_from_payload(payload: dict) -> ApiUser:
        try:
            user = ApiUser(
                username=payload["username"],
                name=payload.get("name"),
                avatar_url=payload.get("avatar_url"),
                bio=payload.get("bio"),
            )
            if not isinstance(user.username, str) or any(
                value is not None and not isinstance(value, str)
                for value in (user.name, user.avatar_url, user.bio)
            ):
                raise TypeError
            return user
        except (KeyError, TypeError) as error:
            raise ApiError("User search returned a malformed user.") from error

    def _request(self, method: str, path_and_query: str, body: bytes = b"") -> dict:
        headers = signed_headers(method, path_and_query, body, self.key_path)
        headers["Accept"] = "application/json"
        headers["User-Agent"] = "dev-null-tty"
        if body:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.origin}{path_and_query}",
            data=body if body else None,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=20) as response:
                response_origin = urlsplit(response.geturl())
                if response_origin.scheme != "https" or response_origin.netloc != urlsplit(self.origin).netloc:
                    raise ApiError("The API response came from an unexpected origin.")
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                response_payload = json.loads(error.read())
                message = response_payload.get("message")
                code = response_payload.get("code")
                if not isinstance(message, str) or not message:
                    message = f"The API returned HTTP {error.code}."
            except (json.JSONDecodeError, AttributeError):
                redirect_location = error.headers.get("Location") if error.code in {301, 302, 303, 307, 308} else None
                if redirect_location:
                    target = urlsplit(redirect_location)
                    if target.scheme == "https" and target.hostname in {"dev-null.sh", "www.dev-null.sh"}:
                        message = f"The signed API request was redirected to {target.netloc}{target.path}; it was not followed."
                    else:
                        message = "The signed API request was redirected to an unexpected destination; it was not followed."
                else:
                    message = f"The API returned HTTP {error.code} without a JSON error response."
                code = None
            raise ApiError(message, code) from error
        except urllib.error.URLError as error:
            raise ApiError(f"Could not reach {self.origin}: {error.reason}") from error
        except json.JSONDecodeError as error:
            raise ApiError("The API returned invalid JSON.") from error
        if not isinstance(payload, dict):
            raise ApiError("The API returned an unexpected response.")
        return payload
