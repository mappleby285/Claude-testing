"""vCard and CSV export tests. vCards are verified with the independent `vobject` parser."""

import csv
import io
import os
from pathlib import Path

import pytest
import vobject

from cardconv.csv_export import CLASSIC, PROFILES, WEB, build_csv, contact_to_row
from cardconv.exporter import atomic_write_bytes, safe_filename_stem, vcf_filename, write_vcard
from cardconv.schema import Contact
from cardconv.vcard import contact_to_vcard, escape_text, fold_line

from .conftest import extraction_dict

CARD_ID = "0123456789abcdef0123456789abcdef"


def contact(**overrides) -> Contact:
    return Contact.model_validate(extraction_dict(**overrides))


TRICKY = dict(
    full_name="Zoë Ångström-Müller",
    given_name="Zoë",
    middle_name="",
    family_name="Ångström-Müller",
    prefix="Dr.",
    suffix="PhD",
    company="Café Lumière; Paris, \"Rive Gauche\" \\ Studio",
    department="R&D, Europe",
    job_title="Directrice générale, Opérations; Stratégie",
    emails=["zoe.angstrom@cafe-lumiere.example", "zoë@café.example"],
    phones=[
        {"label": "work", "number": "+33 1 99 00 12 34 poste 12"},
        {"label": "mobile", "number": "+33 6 39 98 76 54"},
        {"label": "fax", "number": "+33 1 99 00 12 35"},
    ],
    websites=["www.cafe-lumiere.example", "shop.cafe-lumiere.example"],
    address={"street": "12, rue de l’Exemple; Bât. B", "city": "Paris", "region": "Île-de-France",
             "postal_code": "75011", "country": "France"},
    other_text=["Instagram: @cafe.lumière", "Line one\nline two"],
)


# ----------------------------------------------------------------- vCard
def test_escape_text():
    assert escape_text("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"


def test_fold_line_respects_75_octets_and_utf8_boundaries():
    line = "NOTE:" + "é" * 100 + "日本語" * 20
    folded = fold_line(line)
    physical = folded.split("\r\n")
    assert len(physical) > 1
    for i, part in enumerate(physical):
        assert len(part.encode("utf-8")) <= 75
        if i:
            assert part.startswith(" ")
        part.encode("utf-8").decode("utf-8")  # no split characters
    assert "".join(p[1:] if i else p for i, p in enumerate(physical)) == line


def test_vcard_structure_crlf_and_utf8():
    text = contact_to_vcard(contact(**TRICKY), CARD_ID)
    assert text.startswith("BEGIN:VCARD\r\nVERSION:3.0\r\n")
    assert text.endswith("END:VCARD\r\n")
    assert "\n" not in text.replace("\r\n", "")  # only CRLF line endings
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75


@pytest.mark.parametrize("charset", [True, False])
def test_vcard_roundtrip_with_independent_parser(charset):
    c = contact(**TRICKY)
    text = contact_to_vcard(c, CARD_ID, outlook_charset=charset)
    card = vobject.readOne(text)
    assert card.fn.value == "Zoë Ångström-Müller"
    n = card.n.value
    assert (n.family, n.given, n.prefix, n.suffix) == ("Ångström-Müller", "Zoë", "Dr.", "PhD")
    assert card.org.value == ["Café Lumière; Paris, \"Rive Gauche\" \\ Studio", "R&D, Europe"]
    assert card.title.value == "Directrice générale, Opérations; Stratégie"
    emails = [e.value for e in card.contents["email"]]
    assert emails == ["zoe.angstrom@cafe-lumiere.example", "zoë@café.example"]
    tels = {t.value: [x.upper() for x in t.params.get("TYPE", [])] for t in card.contents["tel"]}
    assert "CELL" in tels["+33 6 39 98 76 54"]
    assert "FAX" in tels["+33 1 99 00 12 35"]
    assert "WORK" in tels["+33 1 99 00 12 34 poste 12"]
    adr = card.adr.value
    assert adr.street == "12, rue de l’Exemple; Bât. B"
    assert (adr.city, adr.region, adr.code, adr.country) == ("Paris", "Île-de-France", "75011", "France")
    assert card.url.value == "www.cafe-lumiere.example"
    note = card.note.value
    assert "Instagram: @cafe.lumière" in note
    assert "Line one line two" in note  # newline inside other_text was normalised by the schema
    assert "Additional website: shop.cafe-lumiere.example" in note
    assert card.uid.value.endswith(CARD_ID[20:])
    if charset:
        assert "CHARSET=UTF-8" in text
    else:
        assert "CHARSET" not in text


def test_vcard_unsplit_name_keeps_full_name_and_empty_structure():
    c = contact(full_name="田中 由紀", given_name="", middle_name="", family_name="")
    card = vobject.readOne(contact_to_vcard(c, CARD_ID))
    assert card.fn.value == "田中 由紀"
    assert card.n.value.given == "" and card.n.value.family == ""


def test_vcard_company_only_contact():
    c = contact(full_name="", given_name="", middle_name="", family_name="", company="Example GmbH")
    card = vobject.readOne(contact_to_vcard(c, CARD_ID))
    assert card.fn.value == "Example GmbH"


def test_long_note_is_folded_and_parses():
    c = contact(notes="Met at the trade fair; " * 30)
    text = contact_to_vcard(c, CARD_ID)
    assert vobject.readOne(text).note.value.startswith("Met at the trade fair; Met")


# ------------------------------------------------------------------- CSV
def parse_csv(data: bytes) -> list[dict]:
    assert data.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM ("CSV UTF-8")
    text = data.decode("utf-8-sig")
    assert "\r\n" in text
    return list(csv.DictReader(io.StringIO(text, newline="")))


def test_csv_headers_match_profiles():
    for profile in (CLASSIC, WEB):
        data = build_csv([(CARD_ID, contact())], profile)
        header = data.decode("utf-8-sig").split("\r\n")[0]
        assert next(csv.reader([header])) == PROFILES[profile]


def test_classic_csv_mapping():
    rows = parse_csv(build_csv([(CARD_ID, contact(**TRICKY))], CLASSIC))
    r = rows[0]
    assert r["First Name"] == "Zoë" and r["Last Name"] == "Ångström-Müller"
    assert r["Title"] == "Dr."  # Outlook's Title column is the honorific
    assert r["Job Title"] == "Directrice générale, Opérations; Stratégie"
    assert r["Company"] == "Café Lumière; Paris, \"Rive Gauche\" \\ Studio"
    assert r["Department"] == "R&D, Europe"
    assert r["Business Phone"] == "+33 1 99 00 12 34 poste 12"
    assert r["Mobile Phone"] == "+33 6 39 98 76 54"
    assert r["Business Fax"] == "+33 1 99 00 12 35"
    assert r["E-mail Address"] == "zoe.angstrom@cafe-lumiere.example"
    assert r["E-mail 2 Address"] == "zoë@café.example"
    assert r["Business Street"] == "12, rue de l’Exemple; Bât. B"
    assert r["Business State"] == "Île-de-France"
    assert r["Business Country/Region"] == "France"
    assert r["Web Page"] == "www.cafe-lumiere.example"
    assert "Website: shop.cafe-lumiere.example" in r["Notes"]
    assert "Instagram: @cafe.lumière" in r["Notes"]


def test_web_profile_keeps_unmapped_fields_in_notes():
    r = parse_csv(build_csv([(CARD_ID, contact(**TRICKY))], WEB))[0]
    assert "Job Title" not in r
    notes = r["Notes"]
    assert "Job title: Directrice générale, Opérations; Stratégie" in notes
    assert "Department: R&D, Europe" in notes
    assert "Business address: 12, rue de l’Exemple; Bât. B, Paris, Île-de-France, 75011, France" in notes
    assert "Phone (fax): +33 1 99 00 12 35" in notes


def test_overflow_phones_and_emails_go_to_notes():
    c = contact(
        emails=[f"p{i}@a.example" for i in range(5)],
        phones=[{"label": "work", "number": f"+1 555 010 000{i}"} for i in range(4)],
    )
    r = contact_to_row(c, CARD_ID, CLASSIC)
    assert r["Business Phone"].endswith("0") and r["Business Phone 2"].endswith("1")
    assert "Phone (work): +1 555 010 0002" in r["Notes"] and "Phone (work): +1 555 010 0003" in r["Notes"]
    assert r["E-mail 3 Address"] == "p2@a.example"
    assert "Email: p3@a.example" in r["Notes"] and "Email: p4@a.example" in r["Notes"]


def test_unsplit_name_goes_to_first_name_with_note():
    r = contact_to_row(contact(full_name="Nguyễn Văn An", given_name="", middle_name="", family_name=""), CARD_ID, CLASSIC)
    assert r["First Name"] == "Nguyễn Văn An" and r["Last Name"] == ""
    assert "Name order not confirmed" in r["Notes"]


def test_csv_multiple_contacts_and_newlines_in_notes():
    c1 = contact(notes="line 1\nline 2, with comma and \"quotes\"")
    c2 = contact(**TRICKY)
    rows = parse_csv(build_csv([(CARD_ID, c1), ("f" * 32, c2)], CLASSIC))
    assert len(rows) == 2
    assert rows[0]["Notes"].startswith("line 1\nline 2, with comma and \"quotes\"")


# ------------------------------------------------------- files & names
@pytest.mark.parametrize(
    "name,expected",
    [
        ("Jordan A. Rivera", "Jordan_A._Rivera"),
        ('Bad<>:"/\\|?*Name', "Bad_Name"),
        ("CON", "contact_CON"),
        ("Zoë Ångström", "Zoë_Ångström"),
        ("...", "contact"),
        ("", "contact"),
    ],
)
def test_safe_filename_stem(name, expected):
    assert safe_filename_stem(name) == expected


def test_same_name_contacts_get_distinct_files(tmp_path):
    a = write_vcard(tmp_path, contact(), "a" * 32, 1, True)
    b = write_vcard(tmp_path, contact(), "b" * 32, 1, True)
    assert a != b and a.exists() and b.exists()
    assert vcf_filename(contact(), "a" * 32, 2).endswith("_aaaaaaaa_r2.vcf")


def test_atomic_write_leaves_no_partial_file(tmp_path, monkeypatch):
    target = tmp_path / "out.csv"
    atomic_write_bytes(target, b"old")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_bytes(target, b"new content")
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["out.csv"]  # temp file cleaned up


def test_vcard_file_is_utf8_without_bom(tmp_path):
    path = write_vcard(tmp_path, contact(**TRICKY), CARD_ID, 1, True)
    data = Path(path).read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert vobject.readOne(data.decode("utf-8")).fn.value == "Zoë Ångström-Müller"
