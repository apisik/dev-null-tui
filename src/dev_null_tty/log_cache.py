"""Private, account-scoped persistence for the first page of each journal."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from .api import ApiCommit, ApiError, ApiUser, LogName, SignedApiClient

_CACHE_VERSION = 1
_LOG_NAMES: tuple[LogName, ...] = ("following", "latest", "trending")


def _cache_directory() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "dev-null-tui"


def _write_private_json(path: Path, payload: dict) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".cache-", suffix=".tmp", dir=directory
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            os.fchmod(temporary_file.fileno(), 0o600)
            json.dump(payload, temporary_file, ensure_ascii=True, separators=(",", ":"))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class CachedLog:
    commits: tuple[ApiCommit, ...]
    page_size: int


class StartupProfileCache:
    def __init__(self) -> None:
        self.path = _cache_directory() / "startup-user.json"

    def load(self) -> ApiUser | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _CACHE_VERSION
            or not isinstance(payload.get("user"), dict)
        ):
            raise ValueError("The saved profile cache is malformed.")
        user_payload = payload["user"]
        username = user_payload.get("username")
        name = user_payload.get("name")
        avatar_url = user_payload.get("avatar_url")
        bio = user_payload.get("bio")
        if (
            not isinstance(username, str)
            or not username
            or (name is not None and not isinstance(name, str))
            or (avatar_url is not None and not isinstance(avatar_url, str))
            or (bio is not None and not isinstance(bio, str))
        ):
            raise ValueError("The saved profile cache is malformed.")
        return ApiUser(username, name, avatar_url, bio)

    def save(self, user: ApiUser) -> None:
        _write_private_json(
            self.path,
            {
                "version": _CACHE_VERSION,
                "user": {
                    "username": user.username,
                    "name": user.name,
                    "avatar_url": user.avatar_url,
                    "bio": user.bio,
                },
            },
        )

    def clear_for_user(self, username: str) -> None:
        try:
            cached_user = self.load()
        except ValueError:
            cached_user = None
            self.path.unlink(missing_ok=True)
        if cached_user is not None and cached_user.username.casefold() == username.casefold():
            self.path.unlink(missing_ok=True)
        CommitLogCache(username).delete()


class CommitLogCache:
    def __init__(self, username: str) -> None:
        account_id = hashlib.sha256(username.casefold().encode("utf-8")).hexdigest()
        self.directory = _cache_directory()
        self.path = self.directory / f"logs-{account_id}.json"
        self.username = username

    def load(self) -> dict[LogName, CachedLog]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(payload, dict) or payload.get("version") != _CACHE_VERSION:
            raise ValueError("The saved journal cache has an unsupported format.")
        if payload.get("username") != self.username:
            raise ValueError("The saved journal cache belongs to another account.")
        logs_payload = payload.get("logs")
        if not isinstance(logs_payload, dict):
            raise ValueError("The saved journal cache is malformed.")

        logs: dict[LogName, CachedLog] = {}
        for log_name in _LOG_NAMES:
            entry = logs_payload.get(log_name)
            if entry is None:
                continue
            if not isinstance(entry, dict):
                raise ValueError("The saved journal cache is malformed.")
            page_size = entry.get("page_size")
            commits_payload = entry.get("commits")
            if (
                not isinstance(page_size, int)
                or isinstance(page_size, bool)
                or page_size < 0
                or not isinstance(commits_payload, list)
                or len(commits_payload) > page_size
            ):
                raise ValueError("The saved journal cache is malformed.")
            try:
                commits = tuple(
                    SignedApiClient._commit_from_payload(
                        self._commit_payload_from_cache(item)
                    )
                    for item in commits_payload
                )
            except (ApiError, TypeError, ValueError) as error:
                raise ValueError("The saved journal cache is malformed.") from error
            if len({commit.id for commit in commits}) != len(commits):
                raise ValueError("The saved journal cache contains duplicate commits.")
            logs[log_name] = CachedLog(commits, page_size)
        return logs

    def write(self, logs: dict[LogName, CachedLog]) -> None:
        payload = {
            "version": _CACHE_VERSION,
            "username": self.username,
            "logs": {
                log_name: {
                    "page_size": entry.page_size,
                    "commits": [
                        self._commit_payload_for_cache(commit)
                        for commit in entry.commits[: entry.page_size]
                    ],
                }
                for log_name, entry in logs.items()
            },
        }
        _write_private_json(self.path, payload)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)

    def _commit_payload_for_cache(self, commit: ApiCommit) -> dict:
        quoted_commit = commit.quoted_commit
        return {
            "id": commit.id,
            "deleted_at": commit.created_at if commit.deleted else None,
            "author": {"username": commit.username, "name": commit.author_name},
            "body": commit.body,
            "created_at": commit.created_at,
            "stars_count": commit.stars_count,
            "replies_count": commit.replies_count,
            "starred": commit.starred,
            "images": [
                {
                    "id": image.id,
                    "url": image.url,
                    "width": image.width,
                    "height": image.height,
                    "description": image.description,
                }
                for image in commit.images
            ],
            "quoted_commit_id": commit.quoted_commit_id,
            "quoted_commit": (
                {
                    "id": quoted_commit.id,
                    "username": quoted_commit.username,
                    "author_name": quoted_commit.author_name,
                    "body": quoted_commit.body,
                    "deleted": quoted_commit.deleted,
                }
                if quoted_commit is not None
                else None
            ),
        }

    @staticmethod
    def _commit_payload_from_cache(payload: object) -> dict:
        if not isinstance(payload, dict):
            raise TypeError
        author = payload.get("author")
        if not isinstance(author, dict):
            raise TypeError
        images = payload.get("images")
        if not isinstance(images, list):
            raise TypeError
        quoted_commit = payload.get("quoted_commit")
        if quoted_commit is not None and not isinstance(quoted_commit, dict):
            raise TypeError
        return payload

    def save_log(
        self,
        logs: dict[LogName, CachedLog],
        log_name: LogName,
        commits: tuple[ApiCommit, ...],
        page_size: int,
    ) -> None:
        if page_size < 0:
            raise ValueError("The journal page size cannot be negative.")
        if page_size == 0:
            logs[log_name] = CachedLog((), 0)
            self.write(logs)
            return
        unique_commits: list[ApiCommit] = []
        seen_ids: set[str] = set()
        for commit in commits:
            if commit.id not in seen_ids:
                unique_commits.append(commit)
                seen_ids.add(commit.id)
            if len(unique_commits) == page_size:
                break
        logs[log_name] = CachedLog(tuple(unique_commits), page_size)
        self.write(logs)
