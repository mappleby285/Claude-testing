"""Possible-duplicate detection between contacts.

Signals, strongest first:
  * same personal email address                      -> strong
  * same name and same company                       -> medium
  * same generic email (info@, sales@, ...)          -> weak
  * same phone number (often a shared switchboard)   -> weak
  * same name, different/no company                  -> weak

Nothing is ever merged automatically. Identical images are handled earlier by content
hash and never reach this module.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Iterable

from .schema import Contact
from .validation import phone_digits

STRONG, MEDIUM, WEAK = "strong", "medium", "weak"
_RANK = {STRONG: 3, MEDIUM: 2, WEAK: 1}

GENERIC_LOCAL_PARTS = {
    "info", "information", "sales", "contact", "contacts", "office", "admin", "hello", "hi", "support",
    "enquiries", "enquiry", "inquiries", "inquiry", "mail", "email", "team", "hr", "jobs", "careers",
    "accounts", "billing", "invoices", "reception", "marketing", "service", "services", "help",
    "noreply", "no-reply", "general", "booking", "bookings", "orders", "press", "media", "kontakt",
    "contacto", "bureau", "post", "office1",
}

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|ltd|limited|llc|llp|plc|gmbh|ag|sa|sas|sarl|srl|spa|bv|nv|oy|ab|as|kk|co|corp|corporation|company|pty|pte)\b\.?",
    re.I,
)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def norm_name(contact: Contact) -> str:
    if contact.full_name:
        name = contact.full_name
    else:
        name = " ".join(p for p in (contact.given_name, contact.middle_name, contact.family_name) if p)
    tokens = sorted(_fold(name).split())  # order-insensitive: "Tanaka Yuki" == "Yuki Tanaka"
    return " ".join(tokens)


def norm_company(company: str) -> str:
    # Drop dots first so "K.K." / "S.A." / "Inc." normalise to "kk" / "sa" / "inc".
    return " ".join(_COMPANY_SUFFIXES.sub(" ", _fold((company or "").replace(".", ""))).split())


def norm_email(email: str) -> str:
    return email.strip().casefold()


def is_generic_email(email: str) -> bool:
    local = norm_email(email).split("@", 1)[0]
    return local in GENERIC_LOCAL_PARTS


def norm_phone(number: str) -> str:
    """Compare on the last 9 digits so '+44 20 7946 0000' matches '020 7946 0000'."""
    digits = phone_digits(number)
    return digits[-9:] if len(digits) >= 7 else ""


@dataclass
class DuplicateCandidate:
    other_id: str
    strength: str
    signals: list[str] = field(default_factory=list)
    other_name: str = ""
    other_status: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def compare(new: Contact, existing: Contact) -> tuple[str | None, list[str]]:
    signals: list[str] = []
    strength: str | None = None

    def bump(level: str) -> None:
        nonlocal strength
        if strength is None or _RANK[level] > _RANK[strength]:
            strength = level

    existing_emails = {norm_email(e) for e in existing.emails}
    for email in new.emails:
        if norm_email(email) in existing_emails:
            if is_generic_email(email):
                signals.append(f"same general email ({email})")
                bump(WEAK)
            else:
                signals.append(f"same email ({email})")
                bump(STRONG)

    name_new, name_old = norm_name(new), norm_name(existing)
    comp_new, comp_old = norm_company(new.company), norm_company(existing.company)
    if name_new and name_new == name_old:
        if comp_new and comp_new == comp_old:
            signals.append("same name and company")
            bump(MEDIUM)
        else:
            signals.append("same name")
            bump(WEAK)

    existing_phones = {norm_phone(p.number) for p in existing.phones} - {""}
    for phone in new.phones:
        key = norm_phone(phone.number)
        if key and key in existing_phones:
            signals.append(f"same phone ({phone.number})")
            bump(WEAK)

    return strength, signals


def find_duplicates(
    new: Contact, others: Iterable[tuple[str, str, Contact]]
) -> list[DuplicateCandidate]:
    """others: (card_id, status, contact). Returns candidates, strongest first."""
    found = []
    for other_id, status, contact in others:
        strength, signals = compare(new, contact)
        if strength:
            found.append(
                DuplicateCandidate(other_id, strength, signals, contact.display_name(), status)
            )
    found.sort(key=lambda c: -_RANK[c.strength])
    return found


def needs_decision(candidates: list[dict]) -> bool:
    """Strong or medium matches must be resolved before approval."""
    return any(c.get("strength") in (STRONG, MEDIUM) for c in candidates)
