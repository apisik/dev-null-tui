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
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlsplit

API_ORIGIN = "https://dev-null.sh"
SIGNATURE_NAMESPACE = "devnull-api"
DEVICE_DIRECTORY = Path.home() / ".dev-null-tui"
DEVICE_NAME = f"dev-null-tty on {platform.system()}"
REDIRECT_URI = "devnull://auth/callback"
LogName = Literal["following", "latest", "trending"]


@dataclass(frozen=True)
class ApiUser:
    username: str
    name: str | None
    avatar_url: str | None
    bio: str | None


@dataclass(frozen=True)
class ApiCommit:
    id: str
    username: str
    author_name: str | None
    body: str
    created_at: str
    stars_count: int
    replies_count: int


@dataclass(frozen=True)
class ApiLog:
    commits: tuple[ApiCommit, ...]
    next_cursor: str | None
    is_ranked: bool
    new_commits_count: int
    suggestions: tuple[ApiUser, ...]


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

    def get_log(self, log_name: LogName, cursor: str | None = None) -> ApiLog:
        if log_name not in {"following", "latest", "trending"}:
            raise ValueError(f"Unsupported log: {log_name}")
        path = f"/api/v1/log/{log_name}"
        if cursor is not None:
            path = f"{path}?{urlencode({'cursor': cursor})}"
        payload = self._request("GET", path)
        return self._log_from_payload(payload)

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
    def _commit_from_payload(payload: dict) -> ApiCommit:
        try:
            author = payload["author"]
            commit = ApiCommit(
                id=payload["id"],
                username=author["username"],
                author_name=author.get("name"),
                body=payload["body"],
                created_at=payload["created_at"],
                stars_count=payload["stars_count"],
                replies_count=payload["replies_count"],
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
            if not isinstance(author, dict):
                raise TypeError
            return commit
        except (KeyError, TypeError, ValueError) as error:
            raise ApiError("The API returned a malformed commit in a log.") from error

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
