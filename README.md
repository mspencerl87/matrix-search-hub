# matrix-search-hub

[![Docker Pulls](https://img.shields.io/docker/pulls/mspencerl87/matrix-search-hub)](https://hub.docker.com/r/mspencerl87/matrix-search-hub)

A centralized, multi-user version of [matrix-search](https://github.com/mspencerl87/matrix-search):
one deployment that any employee can sign into with their own company
account, each getting their own searchable index of their own Matrix
message history. Nobody else - including the person running this server -
can read it without that user's own passphrase.

## Screenshots

<table>
<tr>
<td><img src="docs/screenshots/sign-in.png" width="380" alt="Sign-in screen"><br>Sign in</td>
<td><img src="docs/screenshots/unlock.png" width="380" alt="Vault unlock screen"><br>Unlock (after a restart)</td>
</tr>
<tr>
<td><img src="docs/screenshots/search.png" width="380" alt="Search UI with range, sort, and room filters"><br>Search - range/sort/room filters, resync, key import, passphrase change</td>
<td><img src="docs/screenshots/search-results.png" width="380" alt="Search results with highlighted matches"><br>Results with highlighted matches (redacted for this README)</td>
</tr>
</table>

<img src="docs/screenshots/admin-panel.png" width="780" alt="Admin panel showing overview and per-user sync health">

*Admin panel - deployment overview and per-user sync health (redacted for this README).*

## How login works

1. A user clicks **Sign in**, which redirects them to your identity
   provider's real login page with a PKCE challenge and a freshly generated
   Matrix device ID embedded in the OAuth scope, per MSC2967.
2. They authenticate there exactly as they would signing into Element.
3. The provider redirects back to `/auth/callback` with an authorization
   code, exchanged server-side for an access token + refresh token - this
   becomes that user's own dedicated Matrix session/device.
4. The app registers itself as an OAuth client automatically on first
   startup via dynamic client registration (no admin action needed) and
   caches the result in `data/oauth_client.json`.

This only works against a homeserver running **OIDC-native auth**
([MSC2965](https://github.com/matrix-org/matrix-spec-proposals/pull/2965) /
Matrix Authentication Service). Check with:

```bash
curl -s https://<your-server-name>/.well-known/matrix/client
```

If the response has an `org.matrix.msc2965.authentication` block, you're
good. If not, this app can't use your homeserver's auth - use the
single-user `matrix-search` project instead.

Note that `<your-server-name>` above (the domain in user IDs, e.g.
`example.com` for `@you:example.com`) is frequently a **different host**
than `MATRIX_HOMESERVER` (e.g. `matrix.example.com`), since `.well-known`
delegation exists precisely so the client-server API can live somewhere
else. This app needs both: `MATRIX_HOMESERVER` for actual API calls, and
`MATRIX_SERVER_NAME` (defaults to `MATRIX_HOMESERVER` if unset) for this
`.well-known/matrix/client` discovery step specifically. Getting this
wrong is the most common startup failure - it shows up as a JSON decode
error fetching `.well-known/matrix/client`, because the API host still
returns HTTP 200 for that path instead of a clean 404.

## Encryption model - read this before deploying

Every user's messages and Matrix session tokens are stored in a
**per-user, passphrase-encrypted database** (`data/users/<user>/vault.db`,
via SQLCipher). Nobody can read a user's data without that specific
passphrase - not an admin with full filesystem access, not a database
backup, not anyone else at the company. That passphrase:

- is set by the user themselves the first time they use the app (separate
  from their SSO password),
- is never sent anywhere but that one request, and never stored anywhere,
  not even hashed - the only proof it's correct is that it successfully
  decrypts that user's existing vault,
- lives only in server memory, only for as long as that user's sync worker
  is running.

**What this actually defends against:** someone pulling the database, a
backup, or raw files off disk gets nothing readable. A "just export this
person's messages" request has no technical answer unless that person
unlocks it themselves.

**What this does *not* defend against**, and it's worth being honest about
both:
1. Someone directly compelling a user to type their own passphrase under
   pressure - that's a policy/HR/legal problem, not one encryption solves.
2. Whoever controls what code actually runs on this server modifying it to
   capture passphrases as they're typed. If the same people you're
   protecting data from also control deployment of this app, no amount of
   application-level encryption closes that gap - it needs independent
   code review/audit, or hosting this somewhere that party doesn't control.

### What this means day to day

- **First time:** after signing in, a user sets their passphrase and
  indexing starts immediately - full backfill, then live sync, same as
  before.
- **Normal use:** once unlocked, everything behaves exactly as you'd
  expect - instant search, live indexing, no repeated prompts. The
  passphrase stays resident in server memory for the life of the process.
- **After a restart** (deploy, host reboot, crash): every user's worker is
  paused - **nothing auto-resumes**, by design, since resuming would mean
  the key survived the restart somewhere, which defeats the point. Each
  person needs to visit the app and unlock again before their indexing
  picks back up. This is the entire cost of the model: an occasional
  re-unlock, not degraded search.
- **If someone forgets their passphrase:** there is no recovery. Their
  vault is unreadable, by design - the whole guarantee rests on nobody but
  them being able to open it. An admin can only delete their vault file
  (`data/users/<user>/vault.db`) so they can set up a fresh one; the old
  index is gone.
- Users can also click **Lock** to proactively evict their key from server
  memory before walking away from a shared or untrusted machine.
- Users can change their own passphrase from the search UI ("Change your
  vault passphrase" panel) - this requires their *current* passphrase and
  rekeys the vault in place with no data loss. This is different from a
  forgotten passphrase, which nobody, including an admin, can recover or
  reset (see the Admin panel section).

## Search range & retention

Two related but distinct settings:

- **Search range** - a dropdown in the search UI (1 / 3 / 6 / 12 months,
  default 1 month) that limits how far back a given search scans, for
  speed. Options beyond `RETENTION_MONTHS` are hidden since there'd be
  nothing to find past that anyway.
- **Retention** (`RETENTION_MONTHS`, default `12`) - the actual cap on how
  much history is ever stored per user, admin-configured via env var.
  Backfill stops paging back once it reaches messages older than this, and
  a background job prunes anything already stored that ages out over
  time. Raising it only affects newly-indexed and future data - it does
  not retroactively recover messages that were already pruned or never
  backfilled under a lower setting.

This also has a privacy benefit worth noting given the encryption model
above: less decrypted history ever sitting on disk at all, encrypted or
not, is less exposure if anything ever does go wrong.

## Usage metrics

A small stats strip on the search page, visible to every signed-in user
(not just admins):

- Searches performed today and all-time.
- Messages/rooms indexed org-wide (summed across everyone's cached
  counts - see below).
- How many users currently have their vault unlocked, out of the total
  who've ever signed in.

This is deliberately **counts only, never names** - the whole point of
keeping it on the shared page rather than admin-only is that it's meant
to be a fun/transparency number everyone sees, and showing who
specifically is or isn't using the tool would be a real privacy/social-
pressure problem for a page like that. Named per-user status already
lives in the admin panel, which is where it stays.

Implementation notes, since the design choices here matter for accuracy:

- Search counts live in a tiny `daily_metrics` table in `control.db` (one
  row per day, incremented in place) rather than one row per search, so
  it stays a few hundred rows forever regardless of search volume -
  "all-time" is just a `SUM()` over it.
- The org-wide message/room totals are a **sum of last-known per-user
  counts**, cached into `control.db` opportunistically whenever a user's
  own `/api/status` is polled. This means the total reflects each user's
  count as of whenever they were last active, not a live read of every
  vault - which is what makes it computable at all without touching
  anyone's encrypted data. It also means the "rooms" figure is a sum of
  each user's own room memberships, not a deduplicated org-wide room
  count - a room with 20 members indexing it counts 20 times. Treat both
  numbers as a usage/volume indicator, not a precise inventory.

## Recent conversations

The search page shows your 10 most recently active direct messages (left
sidebar) and 10 most recently active rooms (right sidebar), each linking
straight into Element. This is per-user - it only ever reads from your own
unlocked vault, same as search itself.

DM vs. room classification comes from Matrix's own `m.direct` account
data (via nio's `list_direct_rooms()`), refreshed on every full sync - the
same signal Element itself uses, not a guess based on member count. A room
that hasn't been classified yet (e.g. indexed before this feature existed,
not yet through a fresh sync) defaults to the "Rooms" bucket rather than
risking miscategorizing a real DM. Run **Resync now** to refresh
classification immediately instead of waiting for the next automatic sync.

Each entry also shows an avatar - the room's own icon for a group room, or
the other person's profile picture for a DM (nio's `gen_avatar_url`, the
same logic Element uses to decide which to show). Avatars are fetched
through `/api/avatar`, which proxies the request through your own unlocked
session rather than hitting the homeserver's media repo directly from the
browser - modern homeservers require an authenticated request for media,
and this keeps that authentication server-side instead of exposing your
access token to the browser. A room with no avatar set falls back to a
plain initial.

Both sidebars hide below ~1100px viewport width to keep the search column
usable on narrower screens/tablets.

## Unread messages

A separate page (`/unread.html`, linked next to **Admin** in the header,
with a live badge showing how many conversations have something unread)
lists every room with unread activity - meant for a quick "what actually
needs my attention" scan without clicking through Element's room list one
conversation at a time.

Counts come from this account's own read-receipt position, tracked from
the receipt events Matrix sends this app's session (`m.read` and the
*private* `m.read.private` variant - Element sends the private kind by
default) for messages from anyone but yourself, compared against the
account's own indexed messages. Reading a message in Element (or anywhere
else, on any of your devices) clears it here too, on the next sync.

This is *not* the same as nio's built-in
`MatrixRoom.unread_notifications`/`unread_highlights` fields, and
deliberately doesn't use them: those reflect this app's own bot device's
read position specifically, and since this app doesn't mark things read
on that device *automatically*, they'd only ever grow - not what "unread"
means to a human checking in each morning. A room with no read-receipt
seen yet (e.g. one you've never opened since this feature shipped) is
left out of the list entirely rather than shown as fully unread with no
real baseline - open it once anywhere and it'll start being tracked.

"Mentions" is a plain-text heuristic (your Matrix ID's localpart, or a
literal `@room`, appearing as a whole word) rather than a true push-rule
evaluation, since this app has no access to your account's actual push
rules - close to, but not exactly, what Element itself highlights.
Rooms with a mention sort to the top.

### Mark as read - the one thing in this app that writes anything

Every other feature here is strictly read-only - this is the single
exception, so it's worth being explicit about. Each conversation has a
**mark as read** button, and there's a **Mark all as read** button in the
header (with a confirmation prompt, since it can touch a lot of rooms at
once). Clicking either sends a **real Matrix read receipt** (`m.read`),
from this app's own session, for the newest message this app has indexed
in that room.

This is a genuine account-level action, not a local "hide it here"
toggle: since it's a real receipt, it also clears the room's unread badge
in Element (or any other client you use), not just on this page - because
that's what actually solves "let me clean out my unread list in the
morning without clicking through every room in Element individually."
There's no "mark as unread" - Matrix itself doesn't really support putting
a receipt back in time, so the only way back is to actually reread
something, same as in Element.

**Mark all as read** does exactly that, one conversation at a time (never
concurrently), with a short pause between each and a live "(3/12)"-style
progress label on the button - deliberately not a single burst of
simultaneous requests, so it doesn't hammer your homeserver with everyone's
unread count added together at once.

Like the rest of the app, this only reflects (and, here, acts on) the
state of your own unlocked, currently-syncing session - a locked vault
shows nothing here either, for the same reason `/api/status` and search
don't work locked.

## Rooms (Space hierarchy)

A separate page (`/rooms.html`, linked next to **Unread** in the header)
shows your rooms as a collapsible Space > room tree - the actual structure
your homeserver's admins built, not Element's flattened/mixed sidebar.
Expanding a space shows what's inside it, Teams-style; expand/collapse
state is remembered per room in your browser (`localStorage`) so it stays
how you left it between visits. A filter box narrows the tree to matching
names, auto-expanding any space that contains a match.

The hierarchy comes straight from Matrix's own space mechanism, not a
guess - via a direct call to the dedicated Spaces API
(`space_get_hierarchy`, one per joined room, every full resync) rather
than relying on the `m.room.create`/`m.space.child` state events nio
would otherwise deliver through its usual sync-event callbacks. Those
callbacks only fire when nio actually processes a sync response, which -
like this app's backfill - it silently skips entirely if the
server-returned sync token happens to match what's already stored; since
space data is captured nowhere else, a skipped sync would otherwise mean
no hierarchy at all until something coincidentally forced a real one. The
direct API call sidesteps that dependency completely.

- A room is a **Space** (a container of other rooms, not something you
  chat in directly) if that call reports `room_type: "m.space"` for it.
- A space's children come from that same call's `children_state` (mirrors
  the room's own `m.space.child` state events) - the exact mechanism
  Element's own sidebar hierarchy is built from. This is rebuilt from
  scratch on every full resync rather than patched incrementally, since a
  room being *removed* from a space has to be re-derived by re-querying
  current state, not inferred from an absence of some other signal.
- Each node shows an unread count (same source as the Unread page),
  rolled up through a space so a collapsed header tells you whether
  anything inside needs attention without expanding it.
- **Direct messages are intentionally excluded** - Matrix's space concept
  doesn't organize DMs at all, so they stay covered by the "Recent PMs"
  sidebar and the Unread page's PM column instead.

This is a navigation aid, not a chat client: clicking a room opens it in
Element (or matrix.to) the same way every other link in this app does.
Nothing here reads a room's live timeline, sends messages, or marks
anything read.

## Setup

1. Copy the env file:

   ```bash
   cp .env.example .env
   ```

2. Set `MATRIX_HOMESERVER` (the real API base URL - see above for how to
   find it, since it's often not your account's server name), and
   `MATRIX_SERVER_NAME` if that server name differs from it (it usually
   does - see the note above).

3. Set `BASE_URL` to this app's actual public URL, e.g.
   `https://matrix-search.internal.example.com`. This becomes the OAuth
   `redirect_uri` (`{BASE_URL}/auth/callback`), which must be reachable by
   every user's browser and generally needs to be `https://` - put this
   behind a reverse proxy with a real certificate (Caddy/Traefik/nginx)
   rather than exposing plain HTTP directly.

   Once people are using the app, don't change `BASE_URL` without also
   deleting `data/oauth_client.json` to force re-registering the OAuth
   client with the new redirect URI.

4. Generate a session secret:

   ```bash
   openssl rand -hex 32
   ```

   Put the output in `SESSION_SECRET`. Keep it stable - rotating it logs
   everyone out (their vaults are unaffected, they just need to unlock
   again).

5. Optionally adjust `RETENTION_MONTHS` (default `12`) and set
   `ADMIN_USER_IDS` (comma-separated) if anyone should have admin access.

6. Start it:

   ```bash
   docker compose up -d
   docker compose logs -f
   ```

   By default this pulls the prebuilt image from
   [Docker Hub](https://hub.docker.com/r/mspencerl87/matrix-search-hub)
   (published automatically from this repo's `master` branch). If you'd
   rather build from source - to audit exactly what's running, or to test
   a local change - edit `docker-compose.yml`: comment out the `image:`
   line and uncomment `build: .`, then run `docker compose up -d --build`
   instead.

   On first startup you should see `Discovered OIDC issuer: ...` and
   `Dynamically registered new OAuth client ...`. If registration fails
   because the provider has no `registration_endpoint`, an admin needs to
   register a client manually and you set `OAUTH_CLIENT_ID` /
   `OAUTH_CLIENT_SECRET` instead (redirect URI: `{BASE_URL}/auth/callback`).

7. Open `http://<host>:8080` (or wherever you've mapped/proxied it), sign
   in, and set a passphrase.

## Tests

Install the development dependencies and run pytest:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

The test run includes branch coverage and enforces the baseline configured
in `pyproject.toml`. GitHub Actions runs the same command for every pull
request.

## Encrypted Matrix rooms

Separate from the vault passphrase above - this is about Matrix's own
end-to-end encryption for individual rooms. Each user's login creates a
brand-new Matrix device with none of the room keys needed to decrypt their
encrypted (E2EE) rooms yet. From the search UI:

- **Historical messages:** the "Import your Element key export" panel -
  export keys from Element (Settings > Security & Privacy > Export keys)
  and upload the file with its passphrase (a different passphrase from
  the vault one). Triggers a background re-scan that picks up whatever's
  now decryptable.
- **New messages going forward:** each user should verify this app's
  session from their own Element (Settings > Sessions, look for
  `matrix-search-hub`) using their own recovery key, so their other
  devices share future room keys with it automatically.

Unencrypted rooms need none of this - they index automatically for
everyone.

## Troubleshooting

General approach: `docker compose logs -f` while reproducing the problem,
then grep for the relevant symptom below.

**Login / OAuth failures**

- `JSONDecodeError` fetching `.well-known/matrix/client` at startup - a
  `MATRIX_SERVER_NAME`/`MATRIX_HOMESERVER` mixup (see the note under "How
  login works" above). Verify with:
  ```bash
  curl -s https://<your-server-name>/.well-known/matrix/client
  ```
- `Client registration failed ... invalid redirect_uri; invalid client_uri`
  - `client_uri` (defaults to `BASE_URL`) isn't an HTTPS URL your identity
  provider accepts. A bare LAN IP over `http://` typically fails this.
- `Client registration failed ... invalid redirect_uri` (generic, no
  mention of client_uri) - `client_uri` and `redirect_uri` (built from
  `BASE_URL`) must share the same origin. Don't point `OAUTH_CLIENT_URI`
  somewhere else unless you're sure your provider doesn't enforce this.
- Browser shows `ERR_SSL_PROTOCOL_ERROR` on the callback URL - `BASE_URL`
  has `https://` but nothing is actually terminating TLS in front of the
  app. Either fix `BASE_URL` to match reality, or put a real reverse proxy
  with TLS in front and point `BASE_URL` at that (most identity providers
  reject plain-HTTP redirect URIs outright anyway, so you'll usually need
  the proxy regardless).
- `Authorization grant ... already used` - you reloaded or revisited a
  stale callback URL from an earlier attempt. Authorization codes are
  single-use and short-lived; start over from `/auth/login` in a fresh
  tab rather than reloading an old one.
- After fixing a `BASE_URL`/OAuth config issue, the log still says
  `Reusing previously registered OAuth client ...` with the same old
  client ID - `data/oauth_client.json` wasn't actually deleted. It's
  often owned by `root` (created by the Docker daemon), so a plain `rm`
  can silently fail for a non-root user - confirm with `ls -la
  data/oauth_client.json` after deleting, and use `sudo rm -f` if needed.

**Encrypted rooms / a specific message not showing up**

If `docker compose logs | grep -i "backfill complete"` shows `0 events
could not be decrypted` but a message you know exists still isn't
findable:

- Check whether that event is being seen at all:
  ```bash
  docker compose logs | grep -i "UNDECRYPTABLE\|no prev_batch"
  ```
- Every event gets logged individually as it's processed - `indexing
  event`, `SKIP (older than retention cutoff)`, or `UNDECRYPTABLE` - so
  grepping for the room name or a snippet of the message text can confirm
  whether it was ever seen at all versus silently missed.
- Per-room sync summaries (`sync timeline for <room>: N event(s),
  limited=...`) show how many events came back for a room on a given
  sync pass, and whether a `prev_batch` token was present to page further
  back from.
- A stale key export is the most common cause of "this recent message
  isn't found" even when decryption otherwise looks completely healthy -
  a key export only covers sessions that existed at the moment you
  created it. Re-export from Element immediately before importing again
  if you need current coverage.

**General log filters**

```bash
docker compose logs -f                                # live tail
docker compose logs | grep -i error                    # anything that errored
docker compose logs | grep -i "backfill complete"       # per-user summaries + undecryptable counts
docker compose logs | grep -i "backfill failed"         # per-room exceptions during backfill (caught, not fatal)
docker compose logs | grep -i "oauth client"            # confirms fresh vs. reused client registration
docker compose logs | grep -i "unlocked and started"    # confirms a user's vault actually unlocked
```

## Admin panel

Anyone whose Matrix user ID is listed in `ADMIN_USER_IDS` sees an **Admin**
link in the search UI, leading to `/admin.html`. This isn't limited to one
person - `ADMIN_USER_IDS` takes a comma-separated list, so any number of
people can have admin access (`ADMIN_USER_IDS=@a:example.com,@b:example.com`).
It shows, and only shows, metadata:

- Deployment overview: homeserver, base URL, OIDC issuer, OAuth client,
  retention setting.
- A table of every user who's ever signed in: their device ID, whether
  they've set up a vault, whether it's currently unlocked, indexed
  message/room *counts* (only while unlocked, never content), and sync
  health - when their sync last actually succeeded, how many events
  couldn't be decrypted, and the last error if there's one currently.
- **Lock** - force-evicts a user's key from server memory right now,
  without deleting anything. Useful for incident response (e.g. a stolen
  laptop with an active session) without touching their data.
- **Clear index** - wipes a user's search data only (their vault and
  Matrix session/tokens are untouched) and starts a fresh resync
  automatically. Only available while their vault is unlocked, since
  clearing it means writing to their encrypted database. Useful when
  someone's search seems stuck or wrong and a resync alone (self-service,
  via the "Resync now" button they have themselves) doesn't fix it.
- **Deprovision** - permanently deletes a user's vault, indexed messages,
  and Matrix crypto store. Requires typing their user ID to confirm; there
  is no undo. Use this for offboarding, **and** for a forgotten passphrase -
  there is no way to reset or recover a passphrase without knowing the
  current one (see below), so starting over is the only option.

There is deliberately no way for an admin to read a user's messages or
open their vault without their passphrase - that would defeat the entire
point of the encryption model above. Admin here means "can manage
accounts," not "can read anyone's data." This is also why there's no
"reset passphrase" action: changing an encryption key requires already
knowing the current one, so the only two real options for a locked-out
user are (a) they remember it, or (b) Deprovision and start fresh.

### Adding/removing admins

There are two tiers, on purpose:

- **`ADMIN_USER_IDS`** (env var, comma-separated) - a permanent floor.
  Only changeable by editing `.env` and restarting. This exists so a
  mistake made *in the GUI* can never lock everyone out of the admin
  panel - there's always at least this list to fall back on.
- **GUI-managed admins** - the Admins panel lets any current admin add or
  remove additional admins by Matrix user ID, no restart needed, no
  server access needed. This is purely additive on top of the env list,
  never a replacement for it - the two are shown separately in the panel,
  and an env-listed admin can't be removed from the GUI (you'll get an
  error telling you to edit `.env` instead).

A user doesn't need to have signed in yet to be added as an admin - they
just won't appear in the Users table (or be able to use the Admin link)
until they actually do.

### Company logo

The Branding panel lets an admin upload a logo shown on the **sign-in
page**, before anyone has a session - PNG, JPG, WEBP, or SVG, up to 3MB.
There's no required canvas size like 800x600 - a wordmark, a square icon,
whatever your logo actually is will all be scaled down to fit a small
header area (`max-height: 80px`) while keeping its own aspect ratio, so
don't worry about matching a specific pixel size, just keep the file
itself a reasonable size for fast loading.

The file is validated before being saved - real image data is confirmed
via Pillow (raster formats) or a basic sanity check plus a `<script>`
tag rejection (SVG) - and stored at `data/branding/logo.<ext>`, served
publicly and unauthenticated at `/branding/logo.<ext>` (it has to be, to
render on the pre-login screen). Uploading a new one replaces whatever
was there before; **Remove logo** clears it back to no logo.

## Data & security notes

- `data/users/<user>/vault.db` holds that user's decrypted messages and
  Matrix OAuth tokens, encrypted at rest with their passphrase (SQLCipher).
  This is the only place either lives.
- `data/control.db` is intentionally minimal and unencrypted: which user
  IDs have used the app and their device ID (so the UI can show "unlock"
  vs "set up"), the GUI-managed admin list, each user's last-known
  message/room *counts* (not content - see Usage metrics), and daily
  search-count totals. No tokens or message data.
- `data/oauth_client.json` holds this app's own OAuth client secret if one
  was issued. Don't commit it or expose it.
- `data/branding/` holds the uploaded logo file, if any - intentionally
  public (served unauthenticated at `/branding/...`) since it has to
  render on the pre-login screen. Nothing sensitive belongs in it.
- Logging out only clears the browser session cookie - if the vault is
  still unlocked in server memory, the background sync worker keeps
  running so the index stays current. Use **Lock** (or a restart) to
  actually evict a user's key from memory.
- To fully deprovision someone who's left the company, use the admin
  panel's **Deprovision** button (or the API below) rather than deleting
  files by hand - it makes sure their sync worker is stopped first.

## API

- `GET /api/me` — current session's user_id and is_admin, or 401.
- `GET /api/vault-status` — `{exists, unlocked, has_pending_login}` for
  the logged-in user.
- `POST /api/vault/setup` — `{passphrase}`, first-time vault creation.
- `POST /api/vault/unlock` — `{passphrase}`, resumes an existing vault.
- `POST /api/vault/lock` — evicts the key from memory, stops syncing.
- `POST /api/vault/change-passphrase` — `{current_passphrase,
  new_passphrase}`, rekeys the vault in place; 401 if the current
  passphrase is wrong.
- `GET /api/config` — search range options and retention, for the UI.
- `GET /api/metrics` — any signed-in user; org-wide aggregate counts
  (searches today/all-time, indexed messages/rooms, users unlocked/total)
  for the shared stats strip. Never per-user detail.
- `GET /api/rooms` — distinct `{room_id, room_name}` pairs the logged-in
  user has indexed messages from, for the search UI's room filter.
- `GET /api/search?q=...&limit=50&months=1&sort=relevance&room_id=...` —
  search results for the logged-in user's unlocked vault only; 423 if
  locked. `sort` is one of `relevance` (default), `newest`, or `oldest`;
  `room_id` (optional) restricts to one room.
- `GET /api/status` — indexed message/room counts; 423 if locked.
- `GET /api/recent-conversations?limit=10` — the logged-in user's most
  recently active DMs and rooms (separately bucketed, each with a preview
  of the last message, an `avatar_url` pointing at `/api/avatar`, and links
  into Element/matrix.to); 423 if locked.
- `GET /api/avatar?mxc=mxc://...` — proxies a Matrix avatar thumbnail
  through the logged-in user's own session; 400 for a malformed `mxc`
  value, 404 if the homeserver has no thumbnail for it, 423 if locked.
- `GET /api/unread` — every room with unread activity for the logged-in
  user right now, computed from this account's own read-receipt position
  (see "Unread messages" above - not nio's per-device
  `unread_notifications`), each with a last-message preview and links into
  Element/matrix.to; 423 if locked.
- `POST /api/unread/{room_id}/mark-read` — sends a real `m.read` receipt
  for the newest message this app has indexed in that room, from this
  app's own session; 400 if nothing's indexed for that room to point the
  receipt at, 423 if locked.
- `POST /api/unread/mark-all-read` — the same, for every currently-unread
  room; `{status, marked, total}`; 423 if locked.
- `GET /api/rooms-tree` — the logged-in user's non-DM rooms as a Space >
  room hierarchy tree (see "Rooms (Space hierarchy)" above), each node
  with an avatar, a rolled-up unread count, and links into
  Element/matrix.to; 423 if locked.
- `POST /api/resync` — re-runs a full sync + backfill in the background
  for the logged-in user, without needing a key import. Useful if you
  suspect indexing stalled or missed something.
- `POST /api/import-keys` — multipart `file` + `passphrase`, imports a
  Matrix room-key export and triggers the same background re-scan as
  `/api/resync`.
- `GET /api/branding` — public, unauthenticated; `{logo_url}` (or `null`)
  for the sign-in page.
- `POST /api/admin/logo` — admin-only, multipart `file`; validates and
  saves a new logo, replacing any existing one.
- `POST /api/admin/logo/remove` — admin-only, clears the logo.
- `GET /api/admin/overview`, `GET /api/admin/users` — admin-only, metadata
  as described above (the latter includes each user's sync health).
- `GET /api/admin/admins` — admin-only, `{env_admins, dynamic_admins}`.
- `POST /api/admin/admins` — admin-only, `{user_id}`, adds a GUI-managed
  admin.
- `POST /api/admin/admins/{user_id}/remove` — admin-only; 409 if
  `user_id` is set via `ADMIN_USER_IDS` rather than the GUI.
- `POST /api/admin/users/{user_id}/lock`,
  `POST /api/admin/users/{user_id}/clear-index` (409 if that user is
  locked), `POST /api/admin/users/{user_id}/deprovision` — admin-only.
