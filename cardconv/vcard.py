"""vCard 3.0 (RFC 2426) writer.

Implements value escaping, structured values (N, ADR, ORG), UTF-8 output, CRLF line
endings and line folding at 75 octets without splitting multi-byte characters.

Outlook note: classic Outlook for Windows may misread non-ASCII text in vCard 3.0 files
that carry no charset hint. With `outlook_charset=True` (default) a `CHARSET=UTF-8`
parameter is added to properties that contain non-ASCII text. RFC 2426 does not define
that parameter, but common parsers (including the independent `vobject` parser used in
the tests) accept it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .schema import Contact, PhoneLabel

CRLF = "\r\n"
MAX_LINE_OCTETS = 75

_TEL_TYPES = {
    PhoneLabel.work: "WORK,VOICE",
    PhoneLabel.main: "WORK,VOICE,PREF",
    PhoneLabel.mobile: "CELL,VOICE",
    PhoneLabel.fax: "WORK,FAX",
    PhoneLabel.home: "HOME,VOICE",
    PhoneLabel.other: "VOICE",
}


def escape_text(value: str) -> str:
    """Escape a TEXT value (RFC 2426 section 4)."""
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def structured(*components: str) -> str:
    return ";".join(escape_text(c or "") for c in components)


def fold_line(line: str) -> str:
    """Fold a content line so no physical line exceeds 75 octets (UTF-8)."""
    encoded = line.encode("utf-8")
    if len(encoded) <= MAX_LINE_OCTETS:
        return line
    parts: list[str] = []
    current = ""
    current_len = 0
    limit = MAX_LINE_OCTETS
    for ch in line:
        ch_len = len(ch.encode("utf-8"))
        if current_len + ch_len > limit:
            parts.append(current)
            current, current_len = "", 0
            limit = MAX_LINE_OCTETS - 1  # continuation lines start with one space
        current += ch
        current_len += ch_len
    parts.append(current)
    return (CRLF + " ").join(parts)


def _prop(name: str, value: str, params: list[str] | None = None, charset: bool = False) -> str:
    params = list(params or [])
    if charset and any(ord(c) > 127 for c in value):
        params.append("CHARSET=UTF-8")
    head = ";".join([name, *params])
    return fold_line(f"{head}:{value}")


def vcard_notes(contact: Contact, card_id: str) -> str:
    lines: list[str] = []
    if contact.notes:
        lines.append(contact.notes)
    for text in contact.other_text:
        lines.append(f"Other text on card: {text}")
    for site in contact.websites[1:]:
        lines.append(f"Additional website: {site}")
    lines.append(f"Imported from business card (ref {card_id[:8]})")
    return "\n".join(lines)


def contact_to_vcard(
    contact: Contact,
    card_id: str,
    outlook_charset: bool = True,
    rev: datetime | None = None,
) -> str:
    cs = outlook_charset
    fn = contact.display_name()
    rev = rev or datetime.now(timezone.utc)
    lines = ["BEGIN:VCARD", "VERSION:3.0", "PRODID:-//cardconv//Business Card Converter//EN"]
    lines.append(
        _prop(
            "N",
            structured(contact.family_name, contact.given_name, contact.middle_name, contact.prefix, contact.suffix),
            charset=cs,
        )
    )
    lines.append(_prop("FN", escape_text(fn), charset=cs))
    if contact.company or contact.department:
        org = structured(contact.company, contact.department) if contact.department else escape_text(contact.company)
        lines.append(_prop("ORG", org, charset=cs))
    if contact.job_title:
        lines.append(_prop("TITLE", escape_text(contact.job_title), charset=cs))
    for phone in contact.phones:
        if phone.number:
            lines.append(_prop("TEL", escape_text(phone.number), [f"TYPE={_TEL_TYPES[phone.label]}"], charset=cs))
    for i, email in enumerate(contact.emails):
        types = "INTERNET,WORK,PREF" if i == 0 else "INTERNET,WORK"
        lines.append(_prop("EMAIL", escape_text(email), [f"TYPE={types}"], charset=cs))
    if contact.websites:
        lines.append(_prop("URL", escape_text(contact.websites[0]), ["TYPE=WORK"]))
    if not contact.address.is_empty():
        a = contact.address
        lines.append(
            _prop("ADR", structured("", "", a.street, a.city, a.region, a.postal_code, a.country), ["TYPE=WORK"], charset=cs)
        )
    lines.append(_prop("NOTE", escape_text(vcard_notes(contact, card_id)), charset=cs))
    lines.append(f"UID:urn:uuid:{card_id[:8]}-{card_id[8:12]}-{card_id[12:16]}-{card_id[16:20]}-{card_id[20:32]}")
    lines.append(f"REV:{rev.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("END:VCARD")
    return CRLF.join(lines) + CRLF
