"""A Textual client for /dev/null."""

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import sys
import webbrowser
from typing import Literal

from rich.markup import escape
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.events import Click
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

ComposerMode = Literal["new", "reply", "quote"]
_URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


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


class CommitCard(ListItem):
    """A single commit in a log."""

    def __init__(self, commit: ApiCommit) -> None:
        self.commit = commit
        super().__init__(
            LinkedText(
                _commit_display(commit, 600, show_counts=True),
                classes="commit-card",
            )
        )


class NotificationCard(ListItem):
    def __init__(self, notification: ApiNotification) -> None:
        self.notification = notification
        super().__init__(
            LinkedText(
                _notification_display(notification),
                classes="notification-card",
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
                )
            yield TextArea(
                placeholder="What would you like to share?",
                id="commit-body",
            )
            with Horizontal(id="composer-actions"):
                yield Button(
                    "Post (Ctrl+P)",
                    variant="primary",
                    id="submit-commit",
                    disabled=True,
                )
                yield Button("Cancel  (Ctrl+Q)", id="cancel-commit")
            yield Static("", id="composer-status")

    def on_mount(self) -> None:
        self.query_one("#commit-body", TextArea).focus()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "commit-body":
            self.query_one("#submit-commit", Button).disabled = not bool(
                event.text_area.text.strip()
            )

    async def action_cancel(self) -> None:
        await self.app.pop_screen()


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
        Binding("ctrl+r", "reply_to_current", "Reply", show=False, priority=True),
        Binding("u", "parent_comment", "Parent comment", show=False, priority=True),
        Binding("escape", "back", "Back", show=False, priority=True),
        Binding("r", "retry_replies", "Retry replies", show=False, priority=True),
    ]

    def __init__(self, commit: ApiCommit, client: SignedApiClient) -> None:
        super().__init__()
        self.client = client
        self.levels = [CommitThreadLevel(commit)]
        self.ancestor_context: tuple[ApiCommitContext, ...] = ()
        self.parent_navigation_loading = False

    def compose(self) -> ComposeResult:
        with Container(id="commit-detail-dialog"):
            yield Label("COMMIT THREAD", id="commit-detail-title")
            with VerticalScroll(id="thread-history-pane"):
                yield LinkedText(self._thread_display(), id="thread-context")
            yield Label("", id="replies-title")
            yield Static("Loading replies...", id="replies-status")
            yield RepliesListView(id="commit-replies")
            yield Static(
                "Ctrl+j/k scroll ancestors · j/k select reply · Enter follow · Ctrl+R reply · u parent · Esc back · r retry",
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

    def _thread_display(self) -> Text:
        rendered = Text()
        chain: list[ApiCommit | ApiCommitContext] = [
            *self.ancestor_context,
            *(level.commit for level in self.levels),
        ]
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
                    )
                )
        return rendered

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
        self.query_one("#thread-context", LinkedText).update(self._thread_display())
        self.query_one("#replies-title", Label).update(
            f"REPLIES / @{level.commit.username} · {level.commit.replies_count}"
        )
        reply_list = self.query_one("#commit-replies", RepliesListView)
        reply_list.clear()
        reply_list.extend(CommitCard(reply) for reply in level.replies)
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
                reply_list.extend(CommitCard(reply) for reply in page.commits)
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
        if index is None:
            index = 0 if direction > 0 else count - 1
        else:
            index = min(max(index + direction, 0), count - 1)
        reply_list.index = index
        reply_list.focus()
        self.current_level.selected_index = index

    def action_next_reply(self) -> None:
        self._move_reply_selection(1)

    def action_previous_reply(self) -> None:
        self._move_reply_selection(-1)

    def action_reply_to_current(self) -> None:
        level = self.current_level
        parent = level.commit
        reply_list = self.query_one("#commit-replies", RepliesListView)
        index = reply_list.index
        if index is not None and 0 <= index < len(level.replies):
            parent = level.replies[index]
        self.app._begin_composition(parent=parent)

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
) -> Text:
    body = commit.body.strip()
    if len(body) > body_limit:
        body = f"{body[: body_limit - 3].rstrip()}..."
    author = commit.author_name or commit.username
    rendered = Text()
    rendered.append(author, style="bold")
    rendered.append(
        f" @{commit.username} · {_relative_age(commit.created_at)}\n",
        style="dim",
    )
    rendered.append(_linkify_urls(body))
    if show_counts:
        rendered.append(
            f"\n★ {commit.stars_count}   ↩ {commit.replies_count}",
            style="dim",
        )
    return rendered


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


def _notification_display(notification: ApiNotification) -> Text:
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
        titles = {
            "following": "LOG / FOLLOWING",
            "latest": "LOG / LATEST",
            "trending": "LOG / TRENDING",
        }
        self._show_view(f"{log_name}-view", titles[log_name], f"{log_name}-log")
        self._start_log_load(log_name, reset=True)

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
        target = current + direction
        if 0 <= target < len(journals):
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
        status.update("[dim]Submitting your signed commit...[/dim]")
        try:
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
        if reset:
            self.log_cursors[log_name] = None
            self.query_one(f"#{log_name}-list", ListView).clear()
        self.log_versions[log_name] += 1
        version = self.log_versions[log_name]
        self.log_loading[log_name] = True
        self.run_worker(
            self._load_log_page(log_name, self.log_cursors[log_name], reset, version),
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
