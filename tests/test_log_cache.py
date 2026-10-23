from __future__ import annotations

import stat

from dev_null_tty.api import ApiCommit, ApiCommitImage, ApiCommitPreview
from dev_null_tty.log_cache import CommitLogCache


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
    assert stat.S_IMODE(cache.path.stat().st_mode) == 0o600
    assert CommitLogCache("different-user").load() == {}


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
