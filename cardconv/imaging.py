"""Image loading, hashing and preparation for the API.

Original files are only ever read. Preparation happens in memory: EXIF orientation is
applied, transparency is flattened, and large images are downscaled (never upscaled)
with a high-quality filter so small print stays legible.
"""

from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import SUPPORTED_EXTENSIONS

# The API rejects base64 images larger than 5 MB; stay under it with margin.
MAX_ENCODED_BYTES = 4_500_000
Image.MAX_IMAGE_PIXELS = 120_000_000  # allow big scans, still guard against decompression bombs


class ImageError(Exception):
    """The file is not a readable image."""


@dataclass
class PreparedImage:
    media_type: str
    data_b64: str
    width: int
    height: int


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_supported(path: Path | str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def verify_image(data: bytes) -> tuple[int, int]:
    """Fully decode the image to catch truncated or corrupt files. Returns (w, h)."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            return img.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageError(f"Not a readable image ({type(exc).__name__})") from exc


def load_oriented(data: bytes) -> Image.Image:
    """Open image bytes and apply EXIF orientation. Returns an RGB image."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageError(f"Not a readable image ({type(exc).__name__})") from exc
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.getchannel("A"))
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return img


def resize_for_api(img: Image.Image, max_edge: int) -> Image.Image:
    w, h = img.size
    longest = max(w, h)
    if longest <= max_edge:
        return img
    scale = max_edge / longest
    new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return img.resize(new_size, Image.Resampling.LANCZOS)


def prepare_for_api(data: bytes, max_edge: int = 2048) -> PreparedImage:
    img = resize_for_api(load_oriented(data), max_edge)
    quality = 92
    while True:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        encoded = buf.getvalue()
        if len(encoded) * 4 / 3 <= MAX_ENCODED_BYTES or quality <= 60:
            break
        quality -= 8
    while len(encoded) * 4 / 3 > MAX_ENCODED_BYTES:  # extreme case: shrink dimensions
        img = img.resize((int(img.width * 0.85), int(img.height * 0.85)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        encoded = buf.getvalue()
    return PreparedImage(
        media_type="image/jpeg",
        data_b64=base64.standard_b64encode(encoded).decode("ascii"),
        width=img.width,
        height=img.height,
    )


def display_bytes(data: bytes, max_edge: int = 1400) -> bytes:
    """Correctly oriented PNG for showing in the review screen."""
    img = resize_for_api(load_oriented(data), max_edge)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
