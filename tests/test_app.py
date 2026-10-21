import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from textual.binding import Binding
from textual.widgets import ContentSwitcher, Label, Static, TextArea

from dev_null_tty import CommitComposerScreen, DevNullApp
from dev_null_tty.api import (
    ApiError,
    ApiCommit,
    ApiLog,
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
        app.action_show_profile()
        assert app.query_one("#views", ContentSwitcher).current == "profile"


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
        await pilot.pause(0.1)

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
        app.screen.query_one("#commit-body", TextArea).text = "A thoughtful reply."
        await app._submit_commit()
        assert client.submissions[0] == ("A thoughtful reply.", "parent-id", None)

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
        await app._submit_commit()
        assert client.submissions[1] == ("A quote with my comment.", None, "parent-id")

        app.query_one("#latest-list").focus()
        await pilot.press("C")
        assert isinstance(app.screen, CommitComposerScreen)
        app.screen.query_one("#commit-body", TextArea).text = "A new commit."
        await app._submit_commit()
        assert client.submissions[2] == ("A new commit.", None, None)


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
