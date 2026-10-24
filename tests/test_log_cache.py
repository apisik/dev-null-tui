from __future__ import annotations

import stat
import json

from dev_null_tty.api import (
    ApiCommit,
    ApiCommitImage,
    ApiCommitPreview,
    ApiNotification,
    ApiUser,
)
from dev_null_tty.log_cache import (
    CommitLogCache,
    NotificationCache,
    StartupProfileCache,
)


def test_cache_keeps_only_one_page_per_account(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cache = CommitLogCache("octocat")
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
        for index in range(5)
    )

    cache.save_log({}, "latest", commits, page_size=3)

    entry = cache.load()["latest"]
    assert entry.page_size == 3
    assert [commit.id for commit in entry.commits] == [
        "commit-0",
        "commit-1",
        "commit-2",
    ]
    assert stat.S_IMODE(cache.directory.stat().st_mode) == 0o700
    latest_path = cache.path_for("latest")
    assert stat.S_IMODE(latest_path.stat().st_mode) == 0o600
    assert CommitLogCache("different-user").load() == {}

    latest_payload = latest_path.read_bytes()
    cache.save_log({"latest": entry}, "trending", commits[:1], page_size=1)
    assert latest_path.read_bytes() == latest_payload
    assert set(cache.load()) == {"latest", "trending"}


def test_cache_migrates_legacy_combined_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cache = CommitLogCache("octocat")
    commit = ApiCommit(
        "legacy-commit",
        "octocat",
        "The Octocat",
        "A legacy cached entry.",
        "2026-10-20T18:00:00Z",
        0,
        0,
    )
    legacy_payload = {
        "version": 1,
        "username": "octocat",
        "logs": {
            "latest": {
                "page_size": 1,
                "commits": [
                    {
                        "id": commit.id,
                        "deleted_at": None,
                        "author": {"username": commit.username, "name": commit.author_name},
                        "body": commit.body,
                        "created_at": commit.created_at,
                        "stars_count": commit.stars_count,
                        "replies_count": commit.replies_count,
                        "starred": False,
                        "images": [],
                        "quoted_commit_id": None,
                        "quoted_commit": None,
                    }
                ],
            }
        },
    }
    cache.directory.mkdir(parents=True)
    cache.legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    assert cache.load_log("latest") == cache.load()["latest"]
    assert cache.path_for("latest").exists()
    assert not cache.legacy_path.exists()


def test_cache_round_trips_commit_images_and_quote(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cache = CommitLogCache("octocat")
    long_quote = f"{'quoted text ' * 30}FULL_QUOTE_END"
    commit = ApiCommit(
        "commit-1",
        "octocat",
        "The Octocat",
        "An entry with an image and quote.",
        "2026-10-20T18:00:00Z",
        3,
        4,
        starred=True,
        images=(ApiCommitImage("image-1", "https://example.test/image.png", 320, 240, "alt"),),
        quoted_commit_id="quoted-1",
        quoted_commit=ApiCommitPreview(
            "quoted-1",
            "octocat",
            "The Octocat",
            long_quote,
        ),
    )

    cache.save_log({}, "latest", (commit,), page_size=20)

    assert cache.load()["latest"].commits == (commit,)


def test_cache_round_trips_deleted_commit_tombstone(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cache = CommitLogCache("octocat")
    tombstone = ApiCommit(
        "deleted-reply",
        "",
        None,
        "",
        "2026-10-20T18:30:00Z",
        0,
        2,
        deleted=True,
    )

    cache.save_log({}, "latest", (tombstone,), page_size=20)

    assert cache.load()["latest"].commits == (tombstone,)


def test_notification_cache_round_trips_quotes_and_caps_first_page(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cache = NotificationCache("octocat")
    quote_preview = ApiCommitPreview(
        "quoted-1",
        "bob",
        "Bob",
        f"{'Full quote body. ' * 8}QUOTE_END",
    )
    notifications = tuple(
        ApiNotification(
            f"notification-{index}",
            "quoted",
            "2026-10-20T18:00:00Z",
            ApiUser("octocat", "The Octocat", None, None),
            1,
            1,
            1,
            None,
            ApiCommit(
                f"commit-{index}",
                "octocat",
                "The Octocat",
                "A notification commit.",
                "2026-10-20T18:00:00Z",
                0,
                0,
                quoted_commit_id="quoted-1" if index == 0 else None,
            ),
        )
        for index in range(22)
    )

    cache.save(
        notifications,
        "2026-10-20T17:00:00Z",
        {"quoted-1": quote_preview},
    )

    cached = cache.load()
    assert cached is not None
    assert cached.read_at == "2026-10-20T17:00:00Z"
    assert [item.id for item in cached.notifications] == [
        f"notification-{index}" for index in range(20)
    ]
    assert cached.notifications[0].commit is not None
    assert cached.notifications[0].commit.quoted_commit == quote_preview
    assert stat.S_IMODE(cache.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(cache.path.stat().st_mode) == 0o600


def test_rejected_account_clears_notification_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    profile_cache = StartupProfileCache()
    profile_cache.save(ApiUser("octocat", "The Octocat", None, None))
    notifications_cache = NotificationCache("octocat")
    notifications_cache.save((), None)

    profile_cache.clear_for_user("octocat")

    assert notifications_cache.load() is None
