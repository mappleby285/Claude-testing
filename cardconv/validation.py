"""Review flags for a contact.

Each issue has an explicit reason. "review" issues block automatic export and must be
acknowledged (or fixed) before approval; "info" issues are shown but do not block.
These checks catch formatting problems and reported ambiguity. They cannot prove a
transcription is correct.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .schema import Contact, WarningCode

REVIEW = "review"
INFO = "info"

# Pragmatic address check: one @, no spaces, a dot in the domain, sane characters.
# Unicode letters are allowed (internationalised addresses).
_EMAIL_RE = re.compile(r"^[^\s@<>()\[\],;:\"]+@[^\s@<>()\[\],;:\"_]+\.[^\s@<>()\[\],;:\"_.]{2,}$")
_PHONE_ALLOWED = re.compile(r"^[\d\s+\-().\/]*(?:(?:ext\.?|x|extension|poste|durchwahl|dw)\s*\.?\s*\d+)?$", re.I)
_URL_RE = re.compile(r"^(https?://)?[^\s/@]+\.[^\s/@]{2,}(/\S*)?$", re.I)

_WARNING_TEXT = {
    WarningCode.unreadable_text: "Unreadable text",
    WarningCode.ambiguous_field: "Ambiguous field assignment",
    WarningCode.name_structure_uncertain: "Name structure uncertain",
    WarningCode.conflicting_sides: "Front and back disagree",
    WarningCode.multiple_cards: "Several cards in one image",
    WarningCode.not_a_business_card: "Not recognised as a business card",
    WarningCode.instructions_in_image: "Image contains instruction-like text (ignored)",
    WarningCode.other: "Model warning",
}


def issue(code: str, field: str, message: str, severity: str = REVIEW) -> dict[str, Any]:
    return {"code": code, "field": field, "message": message, "severity": severity}


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value.strip())) and ".." not in value


def phone_digits(value: str) -> str:
    """Digits of the main number, excluding any extension."""
    main = re.split(r"(?i)\b(?:ext\.?|x|extension|poste|durchwahl|dw)\s*\.?\s*\d", value)[0]
    return re.sub(r"\D", "", main)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def validate_contact(contact: Contact) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    for w in contact.warnings:
        label = _WARNING_TEXT.get(w.code, "Model warning")
        detail = f": {w.detail}" if w.detail else ""
        issues.append(issue(f"model_{w.code.value}", w.field, f"{label}{detail}"))

    if contact.cards_detected > 1 and not any(w.code == WarningCode.multiple_cards for w in contact.warnings):
        issues.append(issue("model_multiple_cards", "", f"Model saw {contact.cards_detected} cards in the image"))
    if contact.cards_detected == 0 and not any(w.code == WarningCode.not_a_business_card for w in contact.warnings):
        issues.append(issue("no_card", "", "No business card detected"))

    for i, email in enumerate(contact.emails):
        if not is_valid_email(email):
            issues.append(issue("invalid_email", f"emails[{i}]", f"Invalid email format: '{email}'"))

    for i, phone in enumerate(contact.phones):
        digits = phone_digits(phone.number)
        if not phone.number:
            issues.append(issue("empty_phone", f"phones[{i}]", "Phone entry has no number"))
        elif len(digits) < 5 or not _PHONE_ALLOWED.match(phone.number):
            issues.append(issue("odd_phone", f"phones[{i}]", f"Unusual phone format: '{phone.number}'"))

    for i, site in enumerate(contact.websites):
        if not _URL_RE.match(site):
            issues.append(issue("odd_website", f"websites[{i}]", f"Unusual website format: '{site}'"))

    if not contact.display_name() or contact.display_name() == "(no name)":
        issues.append(issue("no_name", "full_name", "No person name or company found"))
    elif not contact.full_name and not (contact.given_name or contact.family_name):
        issues.append(issue("no_person_name", "full_name", "No person name; contact will be filed under the company", INFO))

    if not contact.emails and not contact.phones:
        issues.append(issue("no_contact_method", "", "No email address or phone number found"))

    # Structured name parts should appear in the printed full name.
    if contact.full_name:
        folded_full = _fold(contact.full_name)
        for part_field in ("given_name", "middle_name", "family_name"):
            part = getattr(contact, part_field)
            if part and _fold(part) not in folded_full:
                issues.append(
                    issue("name_mismatch", part_field, f"'{part}' does not appear in the full name '{contact.full_name}'")
                )

    if contact.full_name and not (contact.given_name or contact.family_name):
        if not any(w.code == WarningCode.name_structure_uncertain for w in contact.warnings):
            issues.append(
                issue("name_unsplit", "given_name", "Given/family name not separated; full name will be used as-is", INFO)
            )

    return issues


def blocking(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in issues if i.get("severity") == REVIEW]
