# dev-null-tty

A keyboard-first terminal client for [/dev/null](https://dev-null.sh), built
with [Textual](https://textual.textualize.io/) and managed with
[uv](https://docs.astral.sh/uv/).

The **Following**, **Latest**, and **Trending** journals load real commits from
the signed `/api/v1/log/{log}` endpoint after sign-in. Each journal supports
automatic cursor-based infinite scrolling. Guest users can view the profile
and sign in; journals require an approved device. Signed-in
users can browse a cursor-paginated notifications inbox, publish commits,
reply to a commit, or quote it. Opening the notifications inbox marks
notifications read on /dev/null. Click detected
HTTP(S) links with the mouse to open them in your default browser. The modal
composer shows the original commit above the editor for replies and quotes,
and submits through the signed `POST /api/v1/commits` endpoint. Press
`Ctrl+P` to submit. Press `Ctrl+V` in the composer to edit the draft in Neovim;
the text returns to the composer when Neovim exits successfully. Press `Ctrl+Q`
or `Escape` to close the composer. Typing `@` and part of a username opens a
server-backed dropdown of matching users; press `Down` to focus it and `Enter`
to insert the selected username.
New commits and replies can include up to three JPEG, PNG, or WebP images
(maximum 10 MB each). Press `Ctrl+F` or select **Find images** to open `fzf`
with image files under your home directory. Type to filter, use `Up` / `Down`
to move, `Tab` to select multiple images, and `Enter` to attach them, up to the
three-image limit. Press `Escape` to cancel. The picker uses a full-screen
terminal view and returns to the composer when it closes; `fzf` and `find` must
be installed. Kitty/Ghostty terminals also show an image preview in the picker.
Press `Ctrl+R` or select **Remove images** to open a preview-enabled `fzf`
picker of the attached images. Select one or more and press `Enter` to detach
them; this does not delete the image files. Image previews use
Kitty's graphics protocol and work in Kitty or Ghostty without an external
image-rendering command.
Image uploads use the `/dev/null` CLI signing protocol and require a matching
GitHub-registered SSH key available through
`ssh-agent` or as a private key under `~/.ssh/`. Quotes continue to use the
signed mobile API and do not support image attachments. Journal cards and
thread commits show a paperclip and attachment count. Journal cards show quoted
commits as compact, bordered context cards. In a commit thread, quoted replies
are labeled beside the commenter's name; the replying-to pane also shows the
quoted author's name and text inline. Quoted text wraps to the available width,
and deleted or unavailable targets are labeled where previews are shown. In a
commit thread, press `i` to open
the current commit's expanded image view. A minimap overlay appears
in the upper-left corner. The full image initially fits inside the available
view frame without stretching; zooming moves a frame-sized viewport over it.
Use `n` / `Ctrl+N` to move to
the next / previous image, `=` / `-` to zoom in / out, and `h` / `j` / `k` / `l`
to pan while zoomed. A compact square minimap appears while zoomed, showing the
view position as a dot; the image edge turns red when you reach it. Panning
allows a small buffer beyond the image edges, and the image-view keybinds remain
visible while viewing.
Press `i` again to return to the thread. Images use the
terminal's supported graphics protocol or a colored-character fallback.
In the composer, press `Ctrl+I` to open the full-screen viewer for attached
images. `Ctrl+I` is the only way to toggle the viewer from the composer. The
highlighted help line shows the active image keybinds. The composer action
buttons share one row at the bottom.
Press `Enter` on a selected journal entry to open its full commit and replies.
Use `j` / `k` to navigate replies; if the selected reply is partly obscured,
they first scroll it fully into view. Press `Enter` to follow a reply into its
own thread, `u` to move to the parent comment, and `q` or `Escape` to return to
the previous screen. Commit details show the ancestor and parent-comment
context in a scrollable pane on the left, and the comment being replied to
above its replies on the right. Use `Ctrl+J` / `Ctrl+K` or the mouse wheel to
scroll ancestor history. Use `Alt+J` / `Alt+K` to scroll the replying-to
commit. Press `Ctrl+R` to reply to the thread's main
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

On Omarchy, the app reads the active palette from
`~/.local/state/omarchy/current/theme/colors.toml` at startup and checks for
changes while running, applying updates within about half a second. If that
file is not present, it uses the terminal's ANSI palette. An unreadable or
invalid palette file is reported instead of silently falling back; an invalid
live update leaves the current palette in place.

Keyboard shortcuts:

- `h` / `l` — Previous / next journal
- `r` — Refresh the active journal
- `n` — Open notifications
- `j` / `k` — Next / previous commit or notification
- `j` / `k` (commit thread) — Next / previous reply
- `Enter` — Open the selected commit/notification or follow a selected reply
- `Ctrl+J` / `Ctrl+K` (commit thread) — Scroll ancestor history
- `Alt+J` / `Alt+K` (commit thread) — Scroll the current commit up / down
- `i` (commit thread) — Toggle the current commit's image view
- `Ctrl+I` (composer) — Toggle the attached-image preview while editing
- `n` / `Ctrl+N` (image view) — Next / previous attached image
- `=` / `-` (image view) — Zoom in / out
- `h` / `j` / `k` / `l` (zoomed image) — Pan left / down / up / right
- `Ctrl+R` — Reply to the thread's main commit or selected comment; choose
  attached images to remove in the composer
- `s` — Toggle star on the selected commit or thread comment
- `u` — Move up to the parent comment in a thread
- `q` (commit thread) — Return to the previous screen
- `Escape` — Return to the previous screen
- `Shift+C` — New commit
- `Shift+Q` — Quote selected commit
- `Shift+R` — Reply to selected commit
- `Ctrl+F` (composer) — Find and attach images with `fzf`
- `Ctrl+V` (composer) — Edit the draft in Neovim
- `Down` / `Enter` (composer) — Choose and insert a matching `@mention`
- `Ctrl+P` — Submit the commit, reply, or quote
- `q` — Quit

Journal navigation wraps between Latest, Following, and Trending. Loaded
journals remain visible when you switch away and refresh as soon as you return
to them.

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
git tag v0.3.0
git push origin v0.3.0
```

The workflow checks that the tag matches the project version, runs the tests,
builds the wheel and source distribution, and publishes them to PyPI using
OpenID Connect. No PyPI API token is required.

The upstream project is [nunomaduro/dev-null.sh](https://github.com/nunomaduro/dev-null.sh).