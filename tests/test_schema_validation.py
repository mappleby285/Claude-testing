import pytest
from pydantic import ValidationError

from cardconv.schema import CardExtraction, Contact, extraction_json_schema
from cardconv.validation import blocking, is_valid_email, phone_digits, validate_contact

from .conftest import extraction_dict


def _walk_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for v in node.values():
            yield from _walk_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_objects(v)


def test_json_schema_is_strict_for_structured_outputs():
    schema = extraction_json_schema()
    objects = list(_walk_objects(schema))
    assert len(objects) >= 4  # root, Address, Phone, ExtractionWarning
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    text = str(schema)
    for unsupported in ("'default'", "'minimum'", "'maxLength'"):
        assert unsupported not in text


def test_valid_extraction_parses_and_cleans_whitespace():
    data = extraction_dict(full_name="  Jordan   A.\nRivera ", emails=["a@x.example", "A@x.example", " "])
    e = CardExtraction.model_validate(data)
    assert e.full_name == "Jordan A. Rivera"
    assert e.emails == ["a@x.example"]  # case-insensitive dedupe, blanks dropped


def test_multiple_emails_and_phones_are_kept():
    data = extraction_dict(
        emails=["one@a.example", "two@a.example", "three@a.example", "four@a.example"],
        phones=[{"label": "work", "number": "1"}, {"label": "work", "number": "2"}, {"label": "mobile", "number": "3"}],
    )
    e = CardExtraction.model_validate(data)
    assert len(e.emails) == 4 and len(e.phones) == 3


@pytest.mark.parametrize(
    "bad",
    [
        {"phones": [{"label": "pager-ish", "number": "1"}]},
        {"unexpected_field": "x"},
        {"address": {"street": "x", "planet": "Mars"}},
        {"warnings": [{"code": "made_up", "field": "", "detail": ""}]},
    ],
)
def test_schema_rejects_invalid_data(bad):
    with pytest.raises(ValidationError):
        CardExtraction.model_validate(extraction_dict(**bad))


def test_unicode_preserved():
    e = CardExtraction.model_validate(
        extraction_dict(full_name="Zoë Ångström-Müller", company="Café Lumière SARL", given_name="", family_name="",
                        middle_name="")
    )
    assert e.full_name == "Zoë Ångström-Müller"
    assert Contact.from_extraction(e, ["x.png"]).display_name() == "Zoë Ångström-Müller"


@pytest.mark.parametrize(
    "email,ok",
    [
        ("jordan.rivera@northwind.example.com", True),
        ("first.o'neil+tag@sub.example.org", True),
        ("zoë@café.example", True),
        ("no-at-sign.example.com", False),
        ("two@@example.com", False),
        ("space in@example.com", False),
        ("dots..here@example.com", False),
        ("user@nodot", False),
        ("user@example.c", False),
    ],
)
def test_email_validation(email, ok):
    assert is_valid_email(email) is ok


def test_phone_digits_ignores_extension():
    assert phone_digits("+1 (555) 010-4477 ext. 214") == "15550104477"
    assert phone_digits("020 7946 0321 x12") == "02079460321"


def test_invalid_email_is_flagged_and_blocks():
    c = Contact.model_validate(extraction_dict(emails=["jordan.rivera(at)northwind"]))
    issues = validate_contact(c)
    assert any(i["code"] == "invalid_email" and i["field"] == "emails[0]" for i in blocking(issues))


def test_clean_contact_has_no_blocking_issues():
    assert blocking(validate_contact(Contact.model_validate(extraction_dict()))) == []


def test_model_warnings_become_explicit_review_reasons():
    c = Contact.model_validate(
        extraction_dict(warnings=[
            {"code": "unreadable_text", "field": "phones", "detail": "last digits blurred"},
            {"code": "conflicting_sides", "field": "job_title", "detail": "front says Manager, back says Director"},
        ])
    )
    messages = [i["message"] for i in blocking(validate_contact(c))]
    assert "Unreadable text: last digits blurred" in messages
    assert any(m.startswith("Front and back disagree") for m in messages)


def test_multiple_cards_and_name_mismatch_flagged():
    c = Contact.model_validate(extraction_dict(cards_detected=2, given_name="Jordon"))
    codes = {i["code"] for i in blocking(validate_contact(c))}
    assert {"model_multiple_cards", "name_mismatch"} <= codes


def test_unsplit_international_name_is_not_forced_and_not_blocking():
    c = Contact.model_validate(
        extraction_dict(full_name="田中 由紀", given_name="", middle_name="", family_name="",
                        warnings=[{"code": "name_structure_uncertain", "field": "full_name", "detail": "order unclear"}])
    )
    issues = validate_contact(c)
    assert any(i["code"] == "model_name_structure_uncertain" for i in issues)
    assert c.given_name == "" and c.family_name == ""


def test_missing_contact_method_flagged():
    c = Contact.model_validate(extraction_dict(emails=[], phones=[]))
    assert any(i["code"] == "no_contact_method" for i in blocking(validate_contact(c)))
