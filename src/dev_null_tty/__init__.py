"""A Textual client for /dev/null."""

import argparse
import asyncio
from datetime import datetime, timezone
import sys
import webbrowser
from typing import Literal

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
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


class CommitCard(ListItem):
    """A single commit in a log."""

    def __init__(self, commit: ApiCommit) -> None:
        self.commit = commit
        body = commit.body.strip()
        if len(body) > 600:
            body = f"{body[:597].rstrip()}..."
        author = commit.author_name or commit.username
        super().__init__(
            Static(
                f"[bold]{escape(author)}[/bold] [dim]@{escape(commit.username)} · "
                f"{_relative_age(commit.created_at)}[/dim]\n"
                f"{escape(body)}\n"
                f"[dim]★ {commit.stars_count}   ↩ {commit.replies_count}[/dim]",
                classes="commit-card",
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
                author = self.commit.author_name or self.commit.username
                body = self.commit.body.strip()
                if len(body) > 1200:
                    body = f"{body[:1197].rstrip()}..."
                yield Static(
                    f"[bold]{escape(author)}[/bold] [dim]@{escape(self.commit.username)} · "
                    f"{_relative_age(self.commit.created_at)}[/dim]\n{escape(body)}",
                    id="composer-context",
                )
            yield TextArea(
                placeholder="What would you like to share?",
                id="commit-body",
            )
            with Horizontal(id="composer-actions"):
                yield Button("Post", variant="primary", id="submit-commit")
                yield Button("Cancel  (Ctrl+Q)", id="cancel-commit")
            yield Static("", id="composer-status")

    def on_mount(self) -> None:
        self.query_one("#commit-body", TextArea).focus()

    async def action_cancel(self) -> None:
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
                    yield self._profile_view()
        yield Footer()

    def _sidebar(self) -> Container:
        return Container(
            Static("[bold cyan]/dev/null[/bold cyan]\n[dim]developer logs[/dim]", id="brand"),
            Button("Latest log", id="latest-log", variant="primary"),
            Button("Following", id="following-log"),
            Button("Trending", id="trending-log"),
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
        if isinstance(self.screen, CommitComposerScreen):
            await self.pop_screen()
        else:
            self.exit()

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
        for button_id in ("latest-log", "following-log", "trending-log", "profile"):
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
        if self.query_one("#views", ContentSwitcher).current == "profile":
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

        parent_id = screen.commit.id if screen.mode == "reply" and screen.commit else None
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
