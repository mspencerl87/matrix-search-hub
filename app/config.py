import os

HOMESERVER = os.environ["MATRIX_HOMESERVER"].rstrip("/")

# Public URL this app is reachable at, e.g. https://matrix-search.internal.vates.tech
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
