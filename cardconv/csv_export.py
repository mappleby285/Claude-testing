"""Outlook CSV export profiles.

Two profiles, because the Outlook versions document different column sets:

* ``outlook_classic`` - classic Outlook for Windows (File > Open & Export > Import/Export
  > Import from another program or file > Comma Separated Values). Uses the standard
  English field names that classic Outlook itself writes when exporting contacts,
  so the import wizard maps them automatically, including business address,
  job title and fax.

* ``outlook_web`` - new Outlook for Windows, Outlook on the web and Outlook.com
  (People > Import contacts). Restricted to the column headers listed in Microsoft's
  template for those apps. Fields with no documented column (job title, department,
  business address, fax) are kept in Notes so nothing is lost.

Both files are UTF-8 with a byte-order mark ("CSV UTF-8", as Microsoft recommends)
with CRLF row endings, and English headers (Outlook expects headers in its own
display language).
"""

from __future__ import annotations

import csv
import io
from typing import Iterable

from .schema import Contact, PhoneLabel

CLASSIC = "outlook_classic"
WEB = "outlook_web"

PROFILES: dict[str, list[str]] = {
    CLASSIC: [
        "First Name", "Middle Name", "Last Name", "Title", "Suffix",
        "Company", "Department", "Job Title",
        "Business Street", "Business City", "Business State", "Business Postal Code", "Business Country/Region",
        "Business Phone", "Business Phone 2", "Business Fax", "Mobile Phone", "Home Phone", "Other Phone",
        "E-mail Address", "E-mail 2 Address", "E-mail 3 Address",
        "Web Page", "Notes",
    ],
    WEB: [
        "First Name", "Middle Name", "Last Name", "Title", "Suffix",
        "Company",
        "E-mail Address", "E-mail 2 Address", "E-mail 3 Address",
        "Business Phone", "Business Phone 2", "Mobile Phone", "Home Phone",
        "Web Page", "Notes",
    ],
}

PROFILE_LABELS = {
    CLASSIC: "Classic Outlook for Windows",
    WEB: "New Outlook / Outlook on the web / Outlook.com",
}

# Phone label -> ordered list of columns to fill (per profile).
_PHONE_COLUMNS: dict[str, dict[PhoneLabel, list[str]]] = {
    CLASSIC: {
        PhoneLabel.work: ["Business Phone", "Business Phone 2"],
        PhoneLabel.main: ["Business Phone", "Business Phone 2"],
        PhoneLabel.mobile: ["Mobile Phone"],
        PhoneLabel.fax: ["Business Fax"],
        PhoneLabel.home: ["Home Phone"],
        PhoneLabel.other: ["Other Phone"],
    },
    WEB: {
        PhoneLabel.work: ["Business Phone", "Business Phone 2"],
        PhoneLabel.main: ["Business Phone", "Business Phone 2"],
        PhoneLabel.mobile: ["Mobile Phone"],
        PhoneLabel.fax: [],
        PhoneLabel.home: ["Home Phone"],
        PhoneLabel.other: [],
    },
}


def _format_address(contact: Contact) -> str:
    a = contact.address
    return ", ".join(p for p in (a.street, a.city, a.region, a.postal_code, a.country) if p)


def contact_to_row(contact: Contact, card_id: str, profile: str) -> dict[str, str]:
    columns = PROFILES[profile]
    row = {c: "" for c in columns}
    notes: list[str] = []
    if contact.notes:
        notes.append(contact.notes)

    # Names: never split a name the extractor left unsplit. Outlook needs some name
    # field, so an unsplit full name goes into First Name as-is (noted below).
    if contact.given_name or contact.family_name or contact.middle_name:
        row["First Name"] = contact.given_name
        row["Middle Name"] = contact.middle_name
        row["Last Name"] = contact.family_name
    elif contact.full_name:
        row["First Name"] = contact.full_name
        notes.append("Name order not confirmed; full name kept as printed in First Name.")
    row["Title"] = contact.prefix  # Outlook's "Title" is the honorific (Dr., Ms.)
    row["Suffix"] = contact.suffix
    if contact.full_name:
        composed = " ".join(p for p in (contact.given_name, contact.middle_name, contact.family_name) if p)
        if composed and composed != contact.full_name:
            notes.append(f"Name as printed: {contact.full_name}")
    row["Company"] = contact.company

    if "Job Title" in row:
        row["Job Title"] = contact.job_title
    elif contact.job_title:
        notes.append(f"Job title: {contact.job_title}")
    if "Department" in row:
        row["Department"] = contact.department
    elif contact.department:
        notes.append(f"Department: {contact.department}")

    if not contact.address.is_empty():
        if "Business Street" in row:
            a = contact.address
            row["Business Street"] = a.street
            row["Business City"] = a.city
            row["Business State"] = a.region
            row["Business Postal Code"] = a.postal_code
            row["Business Country/Region"] = a.country
        else:
            notes.append(f"Business address: {_format_address(contact)}")

    for phone in contact.phones:
        if not phone.number:
            continue
        placed = False
        for column in _PHONE_COLUMNS[profile][phone.label]:
            if not row[column]:
                row[column] = phone.number
                placed = True
                break
        if not placed:
            notes.append(f"Phone ({phone.label.value}): {phone.number}")

    email_cols = ["E-mail Address", "E-mail 2 Address", "E-mail 3 Address"]
    for i, email in enumerate(contact.emails):
        if i < len(email_cols):
            row[email_cols[i]] = email
        else:
            notes.append(f"Email: {email}")

    if contact.websites:
        row["Web Page"] = contact.websites[0]
        for site in contact.websites[1:]:
            notes.append(f"Website: {site}")
    for text in contact.other_text:
        notes.append(f"Other text on card: {text}")
    notes.append(f"Imported from business card (ref {card_id[:8]})")
    row["Notes"] = "\n".join(notes)
    return row


def build_csv(contacts: Iterable[tuple[str, Contact]], profile: str) -> bytes:
    """Return the CSV file content (UTF-8 with BOM, CRLF line endings)."""
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=PROFILES[profile], quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writeheader()
    for card_id, contact in contacts:
        writer.writerow(contact_to_row(contact, card_id, profile))
    return buf.getvalue().encode("utf-8-sig")
