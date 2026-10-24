"""Private, account-scoped persistence for the first page of each journal."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from .api import (
    ApiCommit,
    ApiError,
    ApiNotification,
    ApiCommitPreview,
    ApiUser,
    LogName,
    SignedApiClient,
)

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


@dataclass(frozen=True)
class CachedNotifications:
    notifications: tuple[ApiNotification, ...]
    read_at: str | None


def _account_cache_id(username: str) -> str:
    return hashlib.sha256(username.casefold().encode("utf-8")).hexdigest()


def _commit_payload_for_cache(commit: ApiCommit) -> dict:
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
        NotificationCache(username).delete()


class CommitLogCache:
    def __init__(self, username: str) -> None:
        self.account_id = _account_cache_id(username)
        self.directory = _cache_directory()
        self.legacy_path = self.directory / f"logs-{self.account_id}.json"
        self.username = username

    def load(self) -> dict[LogName, CachedLog]:
        logs: dict[LogName, CachedLog] = {}
        for log_name in _LOG_NAMES:
            entry = self.load_log(log_name)
            if entry is not None:
                logs[log_name] = entry
        return logs

    def load_log(self, log_name: LogName) -> CachedLog | None:
        path = self.path_for(log_name)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._migrate_legacy_cache().get(log_name)
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _CACHE_VERSION
            or payload.get("log") != log_name
        ):
            raise ValueError("The saved journal cache has an unsupported format.")
        if payload.get("username") != self.username:
            raise ValueError("The saved journal cache belongs to another account.")
        return self._cached_log_from_payload(payload)

    def write(self, logs: dict[LogName, CachedLog]) -> None:
        for log_name in _LOG_NAMES:
            entry = logs.get(log_name)
            if entry is None:
                self.path_for(log_name).unlink(missing_ok=True)
            else:
                self._write_log(log_name, entry)

    def delete(self) -> None:
        self.legacy_path.unlink(missing_ok=True)
        for log_name in _LOG_NAMES:
            self.path_for(log_name).unlink(missing_ok=True)

    def path_for(self, log_name: LogName) -> Path:
        if log_name not in _LOG_NAMES:
            raise ValueError(f"Unsupported journal cache: {log_name}")
        return self.directory / f"logs-{log_name}-{self.account_id}.json"

    def _write_log(self, log_name: LogName, entry: CachedLog) -> None:
        payload = {
            "version": _CACHE_VERSION,
            "username": self.username,
            "log": log_name,
            "page_size": entry.page_size,
            "commits": [
                _commit_payload_for_cache(commit)
                for commit in entry.commits[: entry.page_size]
            ],
        }
        _write_private_json(self.path_for(log_name), payload)

    def _migrate_legacy_cache(self) -> dict[LogName, CachedLog]:
        try:
            payload = json.loads(self.legacy_path.read_text(encoding="utf-8"))
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
            entry_payload = logs_payload.get(log_name)
            if entry_payload is None:
                continue
            if not isinstance(entry_payload, dict):
                raise ValueError("The saved journal cache is malformed.")
            logs[log_name] = self._cached_log_from_payload(entry_payload)

        for log_name, entry in logs.items():
            path = self.path_for(log_name)
            if not path.exists():
                self._write_log(log_name, entry)
        self.legacy_path.unlink(missing_ok=True)
        return logs

    @classmethod
    def _cached_log_from_payload(cls, payload: object) -> CachedLog:
        if not isinstance(payload, dict):
            raise ValueError("The saved journal cache is malformed.")
        page_size = payload.get("page_size")
        commits_payload = payload.get("commits")
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
                    cls._commit_payload_from_cache(item)
                )
                for item in commits_payload
            )
        except (ApiError, TypeError, ValueError) as error:
            raise ValueError("The saved journal cache is malformed.") from error
        if len({commit.id for commit in commits}) != len(commits):
            raise ValueError("The saved journal cache contains duplicate commits.")
        return CachedLog(commits, page_size)

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
        if log_name not in _LOG_NAMES:
            raise ValueError(f"Unsupported journal cache: {log_name}")
        if page_size < 0:
            raise ValueError("The journal page size cannot be negative.")
        if page_size == 0:
            entry = CachedLog((), 0)
        else:
            unique_commits: list[ApiCommit] = []
            seen_ids: set[str] = set()
            for commit in commits:
                if commit.id not in seen_ids:
                    unique_commits.append(commit)
                    seen_ids.add(commit.id)
                if len(unique_commits) == page_size:
                    break
            entry = CachedLog(tuple(unique_commits), page_size)
        logs[log_name] = entry
        self._write_log(log_name, entry)


class NotificationCache:
    PAGE_SIZE = 20

    def __init__(self, username: str) -> None:
        self.directory = _cache_directory()
        self.path = self.directory / f"notifications-{_account_cache_id(username)}.json"
        self.username = username

    def load(self) -> CachedNotifications | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _CACHE_VERSION
            or payload.get("username") != self.username
        ):
            raise ValueError("The saved notifications cache is malformed.")
        notifications_payload = payload.get("notifications")
        read_at = payload.get("read_at")
        if (
            not isinstance(notifications_payload, list)
            or len(notifications_payload) > self.PAGE_SIZE
            or (read_at is not None and not isinstance(read_at, str))
        ):
            raise ValueError("The saved notifications cache is malformed.")
        try:
            notifications = tuple(
                SignedApiClient._notification_from_payload(item)
                for item in notifications_payload
            )
        except (ApiError, TypeError, ValueError) as error:
            raise ValueError("The saved notifications cache is malformed.") from error
        if len({notification.id for notification in notifications}) != len(notifications):
            raise ValueError("The saved notifications cache contains duplicate entries.")
        return CachedNotifications(notifications, read_at)

    def save(
        self,
        notifications: tuple[ApiNotification, ...],
        read_at: str | None,
        quote_previews: dict[str, ApiCommitPreview] | None = None,
    ) -> None:
        unique_notifications: list[ApiNotification] = []
        seen_ids: set[str] = set()
        for notification in notifications:
            if notification.id not in seen_ids:
                unique_notifications.append(notification)
                seen_ids.add(notification.id)
            if len(unique_notifications) == self.PAGE_SIZE:
                break

        payload = {
            "version": _CACHE_VERSION,
            "username": self.username,
            "read_at": read_at,
            "notifications": [
                {
                    "id": notification.id,
                    "type": notification.type,
                    "created_at": notification.created_at,
                    "actor": (
                        {
                            "username": notification.actor.username,
                            "name": notification.actor.name,
                            "avatar_url": notification.actor.avatar_url,
                            "bio": notification.actor.bio,
                        }
                        if notification.actor is not None
                        else None
                    ),
                    "actors_count": notification.actors_count,
                    "event_count": notification.event_count,
                    "unread_count": notification.unread_count,
                    "reason": notification.reason,
                    "commit": self._notification_commit_payload(
                        notification.commit,
                        quote_previews or {},
                    ),
                }
                for notification in unique_notifications
            ],
        }
        _write_private_json(self.path, payload)

    @staticmethod
    def _notification_commit_payload(
        commit: ApiCommit | None,
        quote_previews: dict[str, ApiCommitPreview],
    ) -> dict | None:
        if commit is None:
            return None
        payload = _commit_payload_for_cache(commit)
        preview = (
            quote_previews.get(commit.quoted_commit_id)
            if commit.quoted_commit_id is not None and commit.quoted_commit is None
            else None
        )
        if preview is not None:
            payload["quoted_commit"] = {
                "id": preview.id,
                "username": preview.username,
                "author_name": preview.author_name,
                "body": preview.body,
                "deleted": preview.deleted,
            }
        return payload

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)
