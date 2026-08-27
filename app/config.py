import os

HOMESERVER = os.environ["MATRIX_HOMESERVER"].rstrip("/")

# Public URL this app is reachable at, e.g. https://matrix-search.internal.example.com
# Must exactly match the redirect_uri registered with the OAuth client (this
# app builds it as f"{BASE_URL}/auth/callback").
BASE_URL = os.environ["BASE_URL"].rstrip("/")

# Long random string used to sign session cookies. Must stay stable across
# restarts (changing it logs everyone out). Generate with: openssl rand -hex 32
SESSION_SECRET = os.environ["SESSION_SECRET"]

DEVICE_NAME = os.environ.get("MATRIX_DEVICE_NAME", "matrix-search-hub")
ELEMENT_URL = os.environ.get("ELEMENT_URL", "https://app.element.io").rstrip("/")

# Only needed if the identity provider has no OAuth dynamic-registration
# endpoint and an admin had to register this app as a client manually.
STATIC_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID")
STATIC_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONTROL_DB_PATH = os.path.join(DATA_DIR, "control.db")
OAUTH_CLIENT_FILE = os.path.join(DATA_DIR, "oauth_client.json")
USERS_DIR = os.path.join(DATA_DIR, "users")

MAX_BACKFILL_PAGES_PER_ROOM = int(os.environ.get("MAX_BACKFILL_PAGES_PER_ROOM", "500"))
TOKEN_REFRESH_MARGIN_SECONDS = 60

# How much history is kept at all, per user, regardless of search range
# selected. Backfill stops paging past this age, and a background job prunes
# anything already stored that ages out. Admins can raise it; there's no
# built-in way for a user to raise it past this for themselves.
RETENTION_MONTHS = int(os.environ.get("RETENTION_MONTHS", "12"))

# Fixed set of choices offered in the search UI's time-range dropdown,
# filtered down to whatever doesn't exceed RETENTION_MONTHS.
SEARCH_RANGE_OPTIONS_MONTHS = (1, 3, 6, 12)
DEFAULT_SEARCH_RANGE_MONTHS = 1

PRUNE_INTERVAL_SECONDS = int(os.environ.get("PRUNE_INTERVAL_SECONDS", str(24 * 3600)))

# Minimum passphrase length for a user's vault. This is the only thing
# protecting their data from anyone with raw disk/backup access, so don't
# let it be trivial.
MIN_VAULT_PASSPHRASE_LENGTH = 12

PENDING_TOKENS_TTL_SECONDS = 900  # time between OIDC callback and vault setup/unlock

# Matrix user IDs (e.g. @admin:example.com) allowed to use the
# admin panel. Empty by default - admin routes 403 for everyone until this
# is set. Admins are just regular users who also appear here; there is no
# separate admin credential, and admin access never grants the ability to
# read anyone's messages or bypass their vault passphrase - only metadata
# (who's registered, locked/unlocked, message counts) and the ability to
# force-lock or deprovision an account.
ADMIN_USER_IDS = {u.strip() for u in os.environ.get("ADMIN_USER_IDS", "").split(",") if u.strip()}
