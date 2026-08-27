# matrix-search-hub

A centralized, multi-user version of [matrix-search](https://github.com/mspencerl87/matrix-search):
one deployment that any employee can sign into with their own company
account, each getting their own searchable index of their own Matrix
message history. Nobody can see anyone else's messages.

Unlike the single-user project (which needs `MATRIX_PASSWORD` or a
manually-copied SSO token in `.env`), this one implements real OIDC login:
users click "Sign in," authenticate against your identity provider exactly
like they would in Element, and get redirected back in - no tokens to copy,
no per-user container config.

## Requirements

This only works against a homeserver running **OIDC-native auth**
([MSC2965](https://github.com/matrix-org/matrix-spec-proposals/pull/2965) /
Matrix Authentication Service). Check with:

```bash
curl -s https://<your-server-name>/.well-known/matrix/client
```

If the response has an `org.matrix.msc2965.authentication` block, you're
good. If not, this app can't use your homeserver's auth - use the
single-user `matrix-search` project instead (with `MATRIX_PASSWORD` or a
manually obtained SSO token).

## How the login works

1. A user clicks **Sign in**, which redirects them to your identity
   provider's real login page (`authorization_endpoint`) with a PKCE
   challenge and a freshly generated Matrix device ID embedded in the
   OAuth scope, per MSC2967.
2. They authenticate there exactly as they would signing into Element.
3. The provider redirects back to `/auth/callback` with an authorization
   code, which the app exchanges server-side for an access token + refresh
   token - this becomes that user's own dedicated Matrix session/device.
4. The app looks up their `user_id` via `/_matrix/client/v3/account/whoami`,
   starts a background sync worker for them (full backfill + live sync into
   their own private SQLite database), and sets a signed session cookie so
   their browser is "logged in" to the search UI.
5. Access tokens are refreshed automatically in the background using the
   refresh token, indefinitely - no re-login needed unless the refresh
   token itself is revoked (e.g. an admin ends the session, or the user
   revokes it from their Element session list).

The app registers itself as an OAuth client automatically on first startup
via dynamic client registration (no admin action needed, unlike setting up
the single-user project's SSO token by hand) and caches the result in
`data/oauth_client.json`.

## Setup

1. Copy the env file:

   ```bash
   cp .env.example .env
   ```

2. Set `MATRIX_HOMESERVER` (the real API base URL - see Requirements
   above for how to find it).

3. Set `BASE_URL` to this app's actual public URL, e.g.
   `https://matrix-search.internal.example.com`. This becomes the OAuth
   `redirect_uri` (`{BASE_URL}/auth/callback`), which must be reachable by
   every user's browser. Most identity providers require this to be
   `https://` in practice - put this behind a reverse proxy with a real
   certificate (Caddy/Traefik/nginx) rather than exposing plain HTTP
   directly, unless your provider explicitly allows HTTP for internal use.

   Once people are actively using the app, don't change `BASE_URL` without
   also deleting `data/oauth_client.json` to force re-registering the
   OAuth client with the new redirect URI - a mismatch here is a common
   source of login failures.

4. Generate a session secret and set it:

   ```bash
   openssl rand -hex 32
   ```

   Put the output in `SESSION_SECRET`. Keep it stable - rotating it logs
   everyone out.

5. Build and start:

   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```

   On first startup you should see `Discovered OIDC issuer: ...` and
   `Dynamically registered new OAuth client ...`. If registration fails
   because the provider has no `registration_endpoint`, an admin needs to
   register a client manually and you set `OAUTH_CLIENT_ID` /
   `OAUTH_CLIENT_SECRET` instead (redirect URI: `{BASE_URL}/auth/callback`).

6. Open `http://<host>:8080` (or wherever you've mapped/proxied it) and
   click **Sign in**.

## Encrypted rooms

Each user's login creates a **brand-new Matrix device**, genuinely owned
by their own OAuth session - so it starts with none of the room keys
needed to decrypt their encrypted (E2EE) rooms, same situation as the
single-user project. Per user, from the search UI itself:

- **Historical messages:** the "Import your Element key export" panel
  under the search box - export keys from Element (Settings > Security &
  Privacy > Export keys) and upload the file with its passphrase. This
  triggers a background re-scan of that user's history that picks up
  whatever's now decryptable.
- **New messages going forward:** each user should verify this app's
  session from their own Element (Settings > Sessions, look for
  `matrix-search-hub`) using their own recovery key, so their other
  devices share future room keys with it automatically.

Unencrypted rooms need none of this - they index automatically for
everyone.

## Data & security notes

This is worth reading before deploying company-wide, not just skimming:

- Every user's messages are stored **decrypted, in plaintext**, in
  `data/users/<user>/search.db`. Centralizing this is a real security
  tradeoff versus everyone running their own single-user instance: a
  compromise of this server exposes every indexed user's message history
  in one place, encrypted rooms included. Make sure whoever owns security
  policy for your org is fine with that before this goes into production
  use.
- `data/control.db` holds every logged-in user's OAuth access + refresh
  tokens. Anyone with read access to that file can act as any of those
  users against the homeserver. Treat `data/` as highly sensitive; back it
  up encrypted if at all, and restrict host-level access to it tightly.
- `data/oauth_client.json` holds this app's own OAuth client secret if one
  was issued. Don't commit it or expose it.
- Logging out only clears the browser session cookie - the background
  sync worker for that user keeps running so their index stays current.
  There's currently no admin UI to fully deprovision a user (stop their
  worker, delete their data); do that manually by removing their row from
  `data/control.db` and their directory under `data/users/` if someone
  leaves the company.

## API

- `GET /api/me` — current session's user_id, or 401.
- `GET /api/search?q=...&limit=50` — search results for the logged-in
  user only.
- `GET /api/status` — indexed message/room counts for the logged-in user.
- `POST /api/import-keys` — multipart `file` + `passphrase`, imports a
  key export for the logged-in user and triggers a background re-scan.
