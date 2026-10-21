# dev-null-tty

A keyboard-first terminal client for [/dev/null](https://dev-null.sh), built
with [Textual](https://textual.textualize.io/) and managed with
[uv](https://docs.astral.sh/uv/).

The **Following**, **Latest**, and **Trending** journals load real commits from
the signed `/api/v1/log/{log}` endpoint after sign-in. Each journal supports
automatic cursor-based infinite scrolling and commit search. Guest users can
view the profile and sign in; journals require an approved device. Signed-in
users can browse a cursor-paginated notifications inbox, publish commits,
reply to a commit, or quote it. Opening the notifications inbox marks
notifications read on /dev/null. Click detected
HTTP(S) links with the mouse to open them in your default browser. The modal
composer shows the original commit above the editor for replies and quotes,
and submits through the signed `POST /api/v1/commits` endpoint. Press
`Ctrl+P` to submit. Press `Ctrl+Q` or `Escape` to close the composer.
Press `Enter` on a selected journal entry to open its full commit and replies.
Use `j` / `k` to navigate replies, `Enter` to follow a reply into its own
thread, `u` to move to the parent comment, and `q` or `Escape` to return to the
previous screen. Commit details show the ancestor and parent-comment context
in a scrollable pane on the left, and the comment being replied to above its
replies on the right. Press `Ctrl+J` / `Ctrl+K` or use the mouse wheel to
scroll the ancestor history. Press `Ctrl+R` to reply to the thread's main
commit or selected comment. Scroll the replies list to load older replies.

The profile's **Sign in with GitHub** action starts the documented PKCE
device authorization flow. To preserve existing sign-ins, the app keeps its
dedicated Ed25519 keypair under `~/.dev-null-tui/` (directory mode `0700`,
private key mode `0600`, no passphrase). It opens the authorization page and,
after approval, signs `POST /api/v1/devices` with the `devnull-api` namespace.
The verifier and state remain in memory. The private key stays on the device
and is never included in API requests.

Before opening the authorization page, the app registers its per-user XDG
desktop handler for `devnull://` (no `sudo`). After approval, the handler
queues the callback for the running TUI, which checks the state before
redeeming the one-time code. Pasting the full `devnull://auth/callback?...`
URL remains available if callback handling is unavailable.

The device key stays in `~/.dev-null-tui/`. On each launch, the app checks that
key with a signed `GET /api/v1/me` and restores the profile when the device is
still approved. A revoked or unapproved key is retained, but the app remains
signed out so you can authorize a device again.

**Log out** removes the local private and public key files and clears the
in-memory session; callback handoff data and the callback-handler registration
are left untouched. This API does not expose a device-revocation endpoint, so
use [Settings → Devices](https://dev-null.sh/settings/devices) to revoke the
device's server-side approval too.

The handler can also be registered manually from the command line:

```bash
uv run dev-null-tty --register-handler
```

This registers a per-user XDG desktop entry for `devnull://` (no `sudo`). The
handler queues the callback in `~/.dev-null-tui/` with owner-only permissions;
the running TUI validates its state and removes the handoff file. Manual paste
remains available if registration is unavailable.

## Run

```bash
uv run dev-null-tty
```

Keyboard shortcuts:

- `h` / `l` — Previous / next journal
- `r` — Refresh the active journal
- `n` — Open notifications
- `j` / `k` — Next / previous commit or notification
- `Enter` — Open the selected commit/notification or follow a selected reply
- `Ctrl+J` / `Ctrl+K` — Scroll commit thread history
- `Ctrl+R` — Reply to the thread's main commit or selected comment
- `u` — Move up to the parent comment in a thread
- `q` (commit thread) — Return to the previous screen
- `Escape` — Return to the previous screen
- `Shift+C` — New commit
- `Shift+Q` — Quote selected commit
- `Shift+R` — Reply to selected commit
- `Ctrl+P` — Submit the commit, reply, or quote
- `q` — Quit

Journal navigation wraps between Latest, Following, and Trending. Loaded
journals remain visible when you switch away; a cached journal refreshes after
you leave it selected for two seconds.

## Test

```bash
uv run pytest
```

## Release

Configure a PyPI trusted publisher for this GitHub repository using the
`.github/workflows/publish.yml` workflow and the `pypi` environment. Update the
version in `pyproject.toml` (and refresh `uv.lock`), then push a matching
version tag, for example:

```bash
git tag v0.2.0
git push origin v0.2.0
```

The workflow checks that the tag matches the project version, runs the tests,
builds the wheel and source distribution, and publishes them to PyPI using
OpenID Connect. No PyPI API token is required.

The upstream project is [nunomaduro/dev-null.sh](https://github.com/nunomaduro/dev-null.sh).