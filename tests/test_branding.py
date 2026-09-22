import base64

import pytest

from app import branding, config


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture(autouse=True)
def branding_dir(tmp_path, monkeypatch):
    directory = tmp_path / "branding"
    monkeypatch.setattr(config, "BRANDING_DIR", str(directory))
    return directory


def test_save_svg_replaces_existing_logo_and_remove(branding_dir):
    branding_dir.mkdir()
    (branding_dir / "logo.png").write_bytes(PNG_1X1)

    name = branding.validate_and_save("brand.svg", "image/svg+xml", b"<svg></svg>")

    assert name == "logo.svg"
    assert branding.current_logo_filename() == "logo.svg"
    assert not (branding_dir / "logo.png").exists()
    branding.remove_logo()
    assert branding.current_logo_filename() is None


def test_content_type_can_supply_a_missing_extension():
    assert branding.validate_and_save("logo", "image/svg+xml", b"<svg></svg>") == "logo.svg"


@pytest.mark.parametrize(
    ("filename", "content_type", "data", "message"),
    [
        ("logo.svg", "image/svg+xml", b"", "empty"),
        ("logo.txt", "text/plain", b"hello", "Unsupported"),
        ("logo.svg", "image/svg+xml", b"not svg", "valid SVG"),
        ("logo.svg", "image/svg+xml", b"<svg><script/></svg>", "script"),
        ("logo.png", "image/png", b"not an image", "valid image"),
    ],
)
def test_rejects_invalid_logos(filename, content_type, data, message):
    with pytest.raises(branding.InvalidLogo, match=message):
        branding.validate_and_save(filename, content_type, data)


def test_rejects_oversized_logo(monkeypatch):
    monkeypatch.setattr(config, "MAX_LOGO_SIZE_BYTES", 2)
    with pytest.raises(branding.InvalidLogo, match="File too large"):
        branding.validate_and_save("logo.svg", "image/svg+xml", b"<svg></svg>")
