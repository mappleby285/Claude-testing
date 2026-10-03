"""Processing pipeline: ingest images, extract, validate, detect duplicates, review, export.

Used by the Streamlit UI, the command line and the watch-folder worker.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import Settings
from .duplicates import MEDIUM, STRONG, find_duplicates, needs_decision
from .exporter import atomic_write_bytes, write_all_csvs, write_csvs, write_vcard
from .extractor import CredentialsError, ExtractionFailure, Extractor
from .imaging import ImageError, prepare_for_api, sha256_bytes, verify_image
from .schema import Contact
from .validation import blocking, validate_contact

log = logging.getLogger(__name__)

_EXPORT_LOCK = threading.Lock()

MERGEABLE_SCALARS = (
    "full_name", "prefix", "given_name", "middle_name", "family_name", "suffix",
    "company", "department", "job_title", "address", "notes",
)
MERGEABLE_LISTS = ("emails", "phones", "websites", "other_text")


class ReviewError(Exception):
    """An action is not allowed in the card's current state (message is user-facing)."""


@dataclass
class AddResult:
    status: str  # "queued", "identical", "requeued", "corrupt"
    card_id: str | None
    message: str


@dataclass
class BatchSummary:
    processed: int = 0
    exported: int = 0
    needs_review: int = 0
    duplicates: int = 0
    failed: int = 0
    stopped_reason: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class ExportResult:
    count: int
    vcards: list[Path]
    csvs: dict[str, Path]
    warnings: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, settings: Settings, store: db.Store | None = None, extractor: Extractor | None = None):
        self.settings = settings
        settings.ensure_dirs()
        self.store = store or db.Store(settings.db_path)
        self.extractor = extractor or Extractor(settings)
        recovered = self.store.recover_interrupted()
        if recovered:
            self.store.log_event("recovered", f"{recovered} interrupted card(s) returned to the queue")

    @property
    def export_mode(self) -> str:
        return self.store.get_setting("export_mode", self.settings.export_mode)

    def set_export_mode(self, mode: str) -> None:
        if mode not in ("review", "auto"):
            raise ValueError(mode)
        self.store.set_setting("export_mode", mode)

    # ------------------------------------------------------------- ingesting
    def _store_image(self, data: bytes, original_name: str) -> tuple[str, bool, str]:
        """Copy image bytes into the local image store. Returns (hash, readable, error)."""
        image_hash = sha256_bytes(data)
        if self.store.get_image(image_hash) is not None:
            return image_hash, True, ""
        try:
            width, height = verify_image(data)
            readable, error = True, ""
        except ImageError as exc:
            width = height = 0
            readable, error = False, str(exc)
        ext = Path(original_name).suffix.lower() or ".img"
        stored = self.settings.image_store / f"{image_hash}{ext}"
        if not stored.exists():
            atomic_write_bytes(stored, data)
        self.store.add_image(image_hash, str(stored), Path(original_name).name, width, height)
        return image_hash, readable, error

    def add_card(self, images: list[tuple[bytes, str]], source: str = "upload") -> AddResult:
        """Register one card (1 image, or 2 images = front and back)."""
        if not 1 <= len(images) <= 2:
            raise ValueError("A card has one or two images")
        hashes: list[str] = []
        errors: list[str] = []
        for data, name in images:
            h, readable, error = self._store_image(data, name)
            hashes.append(h)
            if not readable:
                errors.append(f"{Path(name).name}: {error}")
        label = " + ".join(Path(n).name for _, n in images)

        existing = self.store.find_card_with_images(hashes)
        if existing:
            row = self.store.get_card(existing)
            if row and row.status == db.FAILED and row.error_category not in ("corrupt",):
                self.store.update_card(existing, status=db.QUEUED, error="", error_category="")
                self.store.log_event("requeued", "Identical image re-added after a failure; queued again", existing)
                return AddResult("requeued", existing, f"{label}: previously failed, queued again")
            self.store.log_event("identical_image", "Identical image already processed; no API call made", existing)
            return AddResult("identical", existing, f"{label}: identical image already processed (no new API call)")

        card_id = self.store.create_card(hashes, source=source)
        if errors:
            self.store.update_card(
                card_id, status=db.FAILED, error="; ".join(errors), error_category="corrupt"
            )
            self.store.log_event("failed", "Corrupt or unreadable image", card_id)
            return AddResult("corrupt", card_id, f"{label}: unreadable image")
        self.store.log_event("queued", f"Card queued from {source}", card_id)
        return AddResult("queued", card_id, f"{label}: queued")

    def add_files(self, paths: list[Path], source: str) -> AddResult:
        images = [(Path(p).read_bytes(), Path(p).name) for p in paths]
        return self.add_card(images, source=source)

    def image_files(self, card_id: str) -> list[tuple[Path, str]]:
        """[(local copy, original file name)], front first. Resolved against the image
        store so the app folder can be moved without breaking references."""
        return [
            (self.settings.image_store / Path(stored).name, name)
            for stored, name in self.store.image_paths(card_id)
        ]

    # ------------------------------------------------------------ processing
    def _others_for_duplicates(self, card_id: str) -> list[tuple[str, str, Contact]]:
        """Contacts added *before* this card. Only the newer card of a pair carries the
        duplicate decision, so the same pair is never flagged twice."""
        statuses = (db.NEEDS_REVIEW, db.DUPLICATE_REVIEW, db.APPROVED, db.EXPORTED)
        ordered = self.store.list_cards()  # creation order
        position = {row.id: i for i, row in enumerate(ordered)}
        mine = position.get(card_id, len(ordered))
        return [
            (row.id, row.status, row.contact)
            for row in ordered
            if row.status in statuses and row.contact is not None and position[row.id] < mine
        ]

    def _assess(self, card_id: str, contact: Contact) -> tuple[list[dict], list[dict]]:
        issues = validate_contact(contact)
        dups = [c.to_dict() for c in find_duplicates(contact, self._others_for_duplicates(card_id))]
        return issues, dups

    def process_card(self, card_id: str) -> str:
        """Extract one queued card. Returns the resulting status.
        Raises CredentialsError (card returned to queue) so a batch can stop early."""
        if not self.store.claim_card(card_id):
            row = self.store.get_card(card_id)
            return row.status if row else "missing"
        row = self.store.get_card(card_id)
        assert row is not None
        attempts = row.attempts + 1
        try:
            paths = self.image_files(card_id)
            prepared = [prepare_for_api(stored.read_bytes(), self.settings.max_image_edge) for stored, _ in paths]
            result = self.extractor.extract(prepared)
        except CredentialsError:
            self.store.update_card(card_id, status=db.QUEUED)
            raise
        except ImageError as exc:
            self.store.update_card(card_id, status=db.FAILED, error=str(exc), error_category="corrupt", attempts=attempts)
            self.store.log_event("failed", "Unreadable image", card_id)
            return db.FAILED
        except ExtractionFailure as exc:
            self.store.update_card(
                card_id, status=db.FAILED, error=str(exc), error_category=exc.category, attempts=attempts
            )
            self.store.log_event("failed", f"Extraction failed ({exc.category})", card_id)
            return db.FAILED
        except OSError as exc:
            self.store.update_card(
                card_id, status=db.FAILED, error=f"Could not read stored image ({type(exc).__name__})",
                error_category="io", attempts=attempts,
            )
            return db.FAILED
        except Exception as exc:  # never let one card stop the batch
            log.exception("Unexpected error processing card %s", card_id[:8])
            self.store.update_card(
                card_id, status=db.FAILED, error=f"Unexpected error ({type(exc).__name__})",
                error_category="unexpected", attempts=attempts,
            )
            return db.FAILED

        contact = Contact.from_extraction(result.extraction, [name for _, name in paths])
        issues, dups = self._assess(card_id, contact)
        self.store.update_card(
            card_id,
            extraction=result.extraction.model_dump(mode="json"),
            contact=contact,
            issues=issues,
            duplicates=dups,
            error="",
            error_category="",
            attempts=attempts,
            model=result.model,
            decision="",
        )

        if needs_decision(dups):
            status = db.DUPLICATE_REVIEW
        elif self.export_mode == "auto" and not blocking(issues) and not dups:
            status = db.APPROVED
        else:
            status = db.NEEDS_REVIEW
        self.store.update_card(card_id, status=status)
        self.store.log_event("extracted", f"Extracted; status {status}; {len(blocking(issues))} flag(s)", card_id)

        if status == db.APPROVED:
            self.store.update_card(card_id, decision="auto_approved")
            self.export_cards([card_id], batch_csv=False)
            return db.EXPORTED
        return status

    def process_queue(
        self,
        card_ids: list[str] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        stop: threading.Event | None = None,
    ) -> BatchSummary:
        """Process queued cards one at a time. One failure never stops the others;
        a credentials problem stops the batch (remaining cards stay queued)."""
        if card_ids is None:
            card_ids = [r.id for r in self.store.list_cards((db.QUEUED,))]
        summary = BatchSummary()
        exported_now: list[str] = []
        total = len(card_ids)
        for index, card_id in enumerate(card_ids):
            if stop is not None and stop.is_set():
                summary.stopped_reason = "Stopped by user"
                break
            if progress:
                progress(index, total, card_id)
            try:
                status = self.process_card(card_id)
            except CredentialsError as exc:
                summary.stopped_reason = str(exc)
                break
            summary.processed += 1
            if status == db.EXPORTED:
                summary.exported += 1
                exported_now.append(card_id)
            elif status == db.NEEDS_REVIEW:
                summary.needs_review += 1
            elif status == db.DUPLICATE_REVIEW:
                summary.duplicates += 1
            elif status == db.FAILED:
                summary.failed += 1
                row = self.store.get_card(card_id)
                if row:
                    summary.errors.append(f"{card_id[:8]}: {row.error}")
        if progress:
            progress(total, total, "")
        if exported_now:
            _, warnings = self._write_batch_csv(exported_now)
            summary.errors.extend(warnings)
        return summary

    # ---------------------------------------------------------------- review
    def save_edits(self, card_id: str, contact: Contact) -> CardRowLike:
        row = self._require(card_id)
        if row.status in (db.QUEUED, db.PROCESSING):
            raise ReviewError("This card has not been extracted yet.")
        contact.source_images = row.contact.source_images if row.contact else contact.source_images
        issues, dups = self._assess(card_id, contact)
        dups = self._keep_decided(row, dups)
        fields: dict[str, Any] = {"contact": contact, "issues": issues, "duplicates": dups}
        if row.status == db.EXPORTED or (row.status == db.APPROVED and row.exported_revision):
            # Editing an exported contact creates a new revision file on next export.
            fields["status"] = db.NEEDS_REVIEW
            fields["export_revision"] = row.exported_revision + 1
        elif row.status == db.APPROVED:
            fields["status"] = db.NEEDS_REVIEW
        elif row.status == db.NEEDS_REVIEW and needs_decision(dups) and row.decision != "keep_both":
            fields["status"] = db.DUPLICATE_REVIEW
        elif row.status == db.DUPLICATE_REVIEW and not needs_decision(dups):
            fields["status"] = db.NEEDS_REVIEW
        self.store.update_card(card_id, **fields)
        self.store.log_event("edited", "Edits saved", card_id)
        return self._require(card_id)

    def _keep_decided(self, row: db.CardRow, dups: list[dict]) -> list[dict]:
        if row.decision == "keep_both":
            for d in dups:
                d["resolved"] = True
        return dups

    def approve(self, card_id: str, contact: Contact | None = None, acknowledge: bool = False) -> None:
        row = self._require(card_id)
        if contact is not None:
            row = self.save_edits(card_id, contact)
        if row.contact is None:
            raise ReviewError("Nothing to approve: this card has no extracted data.")
        if row.status not in (db.NEEDS_REVIEW, db.DUPLICATE_REVIEW):
            raise ReviewError(f"Card is '{row.status}', not awaiting review.")
        if needs_decision([d for d in row.duplicates if not d.get("resolved")]):
            raise ReviewError("Resolve the possible duplicate first (keep both, skip, or merge).")
        flags = blocking(row.issues)
        if flags and not acknowledge:
            raise ReviewError(f"{len(flags)} warning(s) need attention. Fix them or confirm you checked them.")
        self.store.update_card(card_id, status=db.APPROVED, decision=row.decision or "approved")
        self.store.log_event("approved", "Approved by user", card_id)

    def skip(self, card_id: str, reason: str = "skipped by user") -> None:
        self._require(card_id)
        self.store.update_card(card_id, status=db.SKIPPED, decision=reason)
        self.store.log_event("skipped", reason, card_id)

    def retry(self, card_id: str) -> str:
        row = self._require(card_id)
        if row.status == db.PROCESSING:
            raise ReviewError("Card is being processed.")
        if row.error_category == "corrupt":
            raise ReviewError("The image file is unreadable; retrying would not help.")
        self.store.update_card(card_id, status=db.QUEUED, error="", error_category="")
        self.store.log_event("retry", "Retry requested", card_id)
        return self.process_card(card_id)

    def keep_both(self, card_id: str) -> None:
        row = self._require(card_id)
        dups = [dict(d, resolved=True) for d in row.duplicates]
        self.store.update_card(card_id, duplicates=dups, decision="keep_both", status=db.NEEDS_REVIEW)
        self.store.log_event("duplicate_keep_both", "Kept as a separate contact", card_id)

    def skip_as_duplicate(self, card_id: str, other_id: str) -> None:
        self._require(card_id)
        self.store.update_card(card_id, status=db.SKIPPED, decision=f"duplicate_of:{other_id}")
        self.store.log_event("duplicate_skipped", f"Skipped as duplicate of {other_id[:8]}", card_id)

    def merge_into(self, card_id: str, other_id: str, fields: list[str]) -> None:
        """Copy the selected fields from this (new) card into an existing contact.
        Scalar fields replace the existing value; list fields are added to it.
        The existing contact is re-queued for export as a new revision; nothing is
        overwritten silently (old export files are kept)."""
        new = self._require(card_id)
        old = self._require(other_id)
        if new.contact is None or old.contact is None:
            raise ReviewError("Both cards need extracted data to merge.")
        merged = old.contact.model_copy(deep=True)
        for f in fields:
            if f in MERGEABLE_SCALARS:
                setattr(merged, f, getattr(new.contact, f))
            elif f in MERGEABLE_LISTS:
                current = list(getattr(merged, f))
                for item in getattr(new.contact, f):
                    if item not in current:
                        current.append(item)
                setattr(merged, f, current)
            else:
                raise ReviewError(f"Field '{f}' cannot be merged.")
        merged.source_images = list(dict.fromkeys(merged.source_images + new.contact.source_images))
        merged.warnings = []
        issues, dups = self._assess(other_id, merged)
        dups = [d for d in dups if d["other_id"] != card_id]
        update: dict[str, Any] = {"contact": merged, "issues": issues, "duplicates": dups}
        if old.status in (db.EXPORTED, db.APPROVED):
            update["status"] = db.NEEDS_REVIEW
            update["export_revision"] = old.exported_revision + 1 if old.exported_revision else 0
            update["decision"] = "keep_both"
            for d in dups:
                d["resolved"] = True
        self.store.update_card(other_id, **update)
        self.store.update_card(card_id, status=db.SKIPPED, decision=f"merged_into:{other_id}")
        self.store.log_event("merged", f"{len(fields)} field(s) merged from {card_id[:8]}", other_id)

    # ---------------------------------------------------------------- export
    def export_cards(self, card_ids: list[str] | None = None, batch_csv: bool = True) -> ExportResult:
        """Export approved cards: one vCard each, plus combined CSVs. A file that cannot
        be written (e.g. a CSV open in Excel) is reported; the other files still export."""
        with _EXPORT_LOCK:
            rows = [
                r for r in self.store.list_cards((db.APPROVED,))
                if card_ids is None or r.id in card_ids
            ]
            vcards: list[Path] = []
            done: list[str] = []
            warnings: list[str] = []
            for row in rows:
                assert row.contact is not None
                revision = max(1, row.export_revision)
                try:
                    path = write_vcard(
                        self.settings.export_dir, row.contact, row.id, revision, self.settings.vcard_outlook_charset
                    )
                except OSError as exc:
                    warnings.append(f"Could not write vCard for card {row.id[:8]} ({type(exc).__name__}); it stays approved.")
                    continue
                self.store.update_card(
                    row.id,
                    status=db.EXPORTED,
                    vcf_path=str(path),
                    exported_at=db.now_iso(),
                    export_revision=revision,
                    exported_revision=revision,
                )
                self.store.log_event("exported", f"Exported revision {revision}", row.id)
                vcards.append(path)
                done.append(row.id)
            csvs: dict[str, Path] = {}
            if done:
                csvs, csv_warnings = self._write_batch_csv(done) if batch_csv else ({}, self._write_all_csv())
                warnings.extend(csv_warnings)
            return ExportResult(len(done), vcards, csvs, warnings)

    def _write_batch_csv(self, card_ids: list[str]) -> tuple[dict[str, Path], list[str]]:
        contacts = [(r.id, r.contact) for r in self.store.list_cards((db.EXPORTED,)) if r.id in card_ids and r.contact]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        paths: dict[str, Path] = {}
        warnings: list[str] = []
        if contacts:
            try:
                paths = write_csvs(self.settings.export_dir, contacts, stamp)
            except OSError as exc:
                warnings.append(f"Could not write the batch CSV files ({type(exc).__name__}).")
        warnings.extend(self._write_all_csv())
        return paths, warnings

    def _write_all_csv(self) -> list[str]:
        contacts = [(r.id, r.contact) for r in self.store.list_cards((db.EXPORTED,)) if r.contact]
        try:
            write_all_csvs(self.settings.export_dir, contacts)
            return []
        except OSError as exc:
            return [
                f"Could not update *_ALL_CONTACTS.csv ({type(exc).__name__}). Close it if it is open in Excel, "
                "then export again or use the per-batch CSV."
            ]

    # --------------------------------------------------------------- summary
    def summary(self) -> dict[str, int]:
        counts = self.store.status_counts()
        return {
            "completed": counts[db.EXPORTED],
            "approved_not_exported": counts[db.APPROVED],
            "pending_review": counts[db.NEEDS_REVIEW],
            "duplicate": counts[db.DUPLICATE_REVIEW],
            "identical_images_skipped": self.store.count_events("identical_image"),
            "skipped": counts[db.SKIPPED],
            "failed": counts[db.FAILED],
            "queued": counts[db.QUEUED] + counts[db.PROCESSING],
        }

    def _require(self, card_id: str) -> db.CardRow:
        row = self.store.get_card(card_id)
        if row is None:
            raise ReviewError("Card not found.")
        return row


CardRowLike = db.CardRow
__all__ = ["Pipeline", "ReviewError", "AddResult", "BatchSummary", "ExportResult", "STRONG", "MEDIUM"]
