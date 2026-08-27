import os

from app import config


def user_dir(user_id: str) -> str:
    safe = user_id.replace("@", "").replace(":", "_")
    return os.path.join(config.USERS_DIR, safe)
