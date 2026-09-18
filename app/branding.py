import io
import os

from app import config

ALLOWED_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


class InvalidLogo(Exception):
    pass


def _ext_for(filename: str, content_type: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in ALLOWED_EXTENSIONS:
        return ext
    for candidate_ext, ct in ALLOWED_EXTENSIONS.items():
        if content_type == ct:
            return candidate_ext
    raise InvalidLogo("Unsupported file type - use PNG, JPG, WEBP, or SVG")


def _clear_existing() -> None:
    if not os.path.isdir(config.BRANDING_DIR):
        return
    for name in os.listdir(config.BRANDING_DIR):
        if name.startswith("logo."):
            os.remove(os.path.join(config.BRANDING_DIR, name))


def current_logo_filename() -> str | None:
    if not os.path.isdir(config.BRANDING_DIR):
        return None
    for name in sorted(os.listdir(config.BRANDING_DIR)):
        if name.startswith("logo."):
            return name
    return None


def validate_and_save(filename: str, content_type: str, data: bytes) -> str:
    if not data:
        raise InvalidLogo("That file is empty")
    if len(data) > config.MAX_LOGO_SIZE_BYTES:
        raise InvalidLogo(f"File too large - max {config.MAX_LOGO_SIZE_BYTES // (1024 * 1024)}MB")

    ext = _ext_for(filename, content_type)

    if ext == ".svg":
        text = data.decode("utf-8", errors="ignore")
        if "<svg" not in text.lower():
            raise InvalidLogo("That doesn't look like a valid SVG file")
        if "<script" in text.lower():
            # img-tag-loaded SVGs can't execute scripts anyway, but reject
            # outright rather than relying on that as the only safeguard.
            raise InvalidLogo("SVGs containing <script> aren't allowed")
    else:
        try:
            from PIL import Image

            Image.open(io.BytesIO(data)).verify()
        except Exception as e:
            raise InvalidLogo("That doesn't look like a valid image file") from e

    os.makedirs(config.BRANDING_DIR, exist_ok=True)
    _clear_existing()
    saved_name = f"logo{ext}"
    with open(os.path.join(config.BRANDING_DIR, saved_name), "wb") as f:
        f.write(data)
    return saved_name


def remove_logo() -> None:
    _clear_existing()
