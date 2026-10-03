"""Writing export files safely: Windows-safe unique filenames and atomic writes."""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

from .csv_export import CLASSIC, WEB, build_csv
from .schema import Contact
from .vcard import contact_to_vcard

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename_stem(name: str, max_len: int = 60) -> str:
    """Keep letters (including accented/non-Latin), digits, spaces, '-', '_' and '.'.
    Remove characters Windows forbids and trailing dots/spaces."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = "".join(c for c in name if c.isalnum() or c in " -_.'&")
    name = re.sub(r"\s+", "_", name.strip())
    name = name.strip("._ ")[:max_len].rstrip("._ ")
    if not name or name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = f"contact_{name}" if name else "contact"
    return name


def vcf_filename(contact: Contact, card_id: str, revision: int) -> str:
    stem = safe_filename_stem(contact.display_name())
    suffix = f"_r{revision}" if revision > 1 else ""
    return f"{stem}_{card_id[:8]}{suffix}.vcf"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write to a temporary file in the same folder, fsync, then rename over the target.
    An interruption leaves either the old file or no file, never a partial one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=path.suffix, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_vcard(export_dir: Path, contact: Contact, card_id: str, revision: int, outlook_charset: bool) -> Path:
    path = Path(export_dir) / "vcards" / vcf_filename(contact, card_id, revision)
    text = contact_to_vcard(contact, card_id, outlook_charset=outlook_charset)
    atomic_write_bytes(path, text.encode("utf-8"))
    return path


def write_csvs(export_dir: Path, contacts: list[tuple[str, Contact]], stamp: str | None = None) -> dict[str, Path]:
    """Write one CSV per profile for this batch, plus refresh the 'all contacts' files."""
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    out: dict[str, Path] = {}
    for profile in (CLASSIC, WEB):
        path = Path(export_dir) / "csv" / f"{profile}_{stamp}.csv"
        atomic_write_bytes(path, build_csv(contacts, profile))
        out[profile] = path
    return out


def write_all_csvs(export_dir: Path, contacts: list[tuple[str, Contact]]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for profile in (CLASSIC, WEB):
        path = Path(export_dir) / "csv" / f"{profile}_ALL_CONTACTS.csv"
        atomic_write_bytes(path, build_csv(contacts, profile))
        out[profile] = path
    return out
