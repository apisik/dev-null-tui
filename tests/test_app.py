import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import MouseScrollDown
from textual.widgets import ContentSwitcher, Label, Static, TextArea

from dev_null_tty import (
    CommitDetailScreen,
    CommitComposerScreen,
    DevNullApp,
    LinkedText,
    LinkedText,
    NotificationDetailScreen,
    _commit_display,
    _linkify_urls,
)
from dev_null_tty.api import (
    ApiError,
    ApiCommit,
    ApiCommitContext,
    ApiCommitDetail,
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
async def test_cached_journals_refresh_only_after_selection_settles() -> None:
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
        await pilot.pause(1.1)
        assert sum(name == "latest" for name, _ in client.calls) == 1

        app._show_log("following")
        assert app.query_one("#following-list").children[0].commit.id == "following-1"
        await pilot.pause(1.1)
        assert sum(name == "latest" for name, _ in client.calls) == 1
        assert sum(name == "following" for name, _ in client.calls) == 1

        await pilot.pause(1.1)
        await wait_for_load("following", 2)
        assert sum(name == "latest" for name, _ in client.calls) == 1
        assert app.query_one("#following-list").children[0].commit.id == "following-2"


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
        assert not app.query_one("#search").display
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
async def test_search_filters_commits() -> None:
    app = DevNullApp()
    async with app.run_test() as pilot:
        class FakeClient:
            def get_log(self, log_name, cursor=None) -> ApiLog:
                return ApiLog(
                    (
                        ApiCommit(
                            "commit-1",
                            "octocat",
                            "The Octocat",
                            "Building in public.",
                            "2026-10-20T18:00:00Z",
                            12,
                            3,
                        ),
                    ),
                    None,
                    False,
                    0,
                    (),
                )

        app.api_client = FakeClient()
        app._show_log("latest")
        await pilot.pause()
        app.query_one("#search").value = "octocat"
        await pilot.pause()
        assert len([item for item in app.query("CommitCard") if item.display]) == 1
        app.query_one("#search").value = "not present"
        await pilot.pause()
        assert not any(item.display for item in app.query("CommitCard"))


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
        assert len(app.screen.current_level.replies) == 12
        assert len(replies.children) == 12
        assert client.reply_cursors == [("parent-id", None)]

        await pilot.press("j", "j", "k")
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
        replies.scroll_y = replies.max_scroll_y
        await pilot.pause(0.1)
        assert client.reply_cursors == [("parent-id", None), ("parent-id", "older")]
        assert len(replies.children) == 13

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
    )


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
