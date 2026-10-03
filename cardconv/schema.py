"""Validated data model for extracted business-card contacts.

`CardExtraction` is what the model must return (its JSON schema is sent to the API as
a structured-output format). `Contact` is the editable record the app stores and
exports; it adds source references and free-form notes.
"""

from __future__ import annotations

import copy
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PhoneLabel(str, Enum):
    work = "work"
    mobile = "mobile"
    fax = "fax"
    home = "home"
    main = "main"  # company switchboard / general line
    other = "other"


class WarningCode(str, Enum):
    unreadable_text = "unreadable_text"
    ambiguous_field = "ambiguous_field"
    name_structure_uncertain = "name_structure_uncertain"
    conflicting_sides = "conflicting_sides"
    multiple_cards = "multiple_cards"
    not_a_business_card = "not_a_business_card"
    instructions_in_image = "instructions_in_image"
    other = "other"


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Phone(_Base):
    label: PhoneLabel = Field(description="work, mobile, fax, home, main (company switchboard) or other")
    number: str = Field(
        description="Exactly as printed, including any + country prefix, spaces, punctuation "
        "and extension (e.g. 'ext. 204'). Never add a country code that is not printed."
    )

    @field_validator("number", mode="before")
    @classmethod
    def _number(cls, v: Any) -> str:
        return _clean(v)


class Address(_Base):
    street: str = Field(default="", description="Street lines as printed; join multiple lines with ', '")
    city: str = ""
    region: str = Field(default="", description="State, province, county or region")
    postal_code: str = ""
    country: str = Field(default="", description="Only if printed on the card; never inferred")

    @field_validator("*", mode="before")
    @classmethod
    def _strings(cls, v: Any) -> str:
        return _clean(v)

    def is_empty(self) -> bool:
        return not any((self.street, self.city, self.region, self.postal_code, self.country))


class ExtractionWarning(_Base):
    code: WarningCode
    field: str = Field(description="Affected field name, or '' if general")
    detail: str = Field(description="Short explanation of the specific problem")

    @field_validator("field", "detail", mode="before")
    @classmethod
    def _strings(cls, v: Any) -> str:
        return _clean(v)


_NAME_FIELDS = (
    "full_name", "prefix", "given_name", "middle_name", "family_name", "suffix",
    "company", "department", "job_title",
)


class CardExtraction(_Base):
    """Structured output requested from the model."""

    cards_detected: int = Field(
        description="Number of distinct business cards visible in the images (front and back of the same card count as one)"
    )
    full_name: str = Field(default="", description="Person's name exactly as printed")
    prefix: str = Field(default="", description="Honorific such as Dr., Mr., Prof. if printed")
    given_name: str = Field(default="", description="Only when the name structure is clear; otherwise ''")
    middle_name: str = ""
    family_name: str = Field(default="", description="Only when the name structure is clear; otherwise ''")
    suffix: str = Field(default="", description="e.g. Jr., III, PhD, if printed after the name")
    company: str = ""
    department: str = ""
    job_title: str = ""
    emails: list[str] = Field(default_factory=list)
    phones: list[Phone] = Field(default_factory=list)
    websites: list[str] = Field(default_factory=list)
    address: Address = Field(default_factory=Address)
    other_text: list[str] = Field(
        default_factory=list,
        description="Other contact-relevant text that fits no field (social handles, second address, licence numbers)",
    )
    warnings: list[ExtractionWarning] = Field(default_factory=list)

    @field_validator(*_NAME_FIELDS, mode="before")
    @classmethod
    def _strings(cls, v: Any) -> str:
        return _clean(v)

    @field_validator("emails", "websites", "other_text", mode="before")
    @classmethod
    def _string_lists(cls, v: Any) -> list[str]:
        items = [_clean(x) for x in (v or [])]
        seen: set[str] = set()
        out = []
        for item in items:
            if item and item.casefold() not in seen:
                seen.add(item.casefold())
                out.append(item)
        return out

    @field_validator("cards_detected", mode="before")
    @classmethod
    def _cards(cls, v: Any) -> int:
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 1


class Contact(CardExtraction):
    """Editable record stored in the database and used for export."""

    cards_detected: int = 1
    notes: str = Field(default="", description="User notes, exported to the Notes field")
    source_images: list[str] = Field(default_factory=list)

    @field_validator("notes", mode="before")
    @classmethod
    def _notes(cls, v: Any) -> str:
        return (str(v) if v is not None else "").strip()

    @classmethod
    def from_extraction(cls, extraction: CardExtraction, source_images: list[str]) -> "Contact":
        return cls(**extraction.model_dump(), source_images=source_images)

    def display_name(self) -> str:
        if self.full_name:
            return self.full_name
        parts = [self.given_name, self.middle_name, self.family_name]
        joined = " ".join(p for p in parts if p)
        return joined or self.company or "(no name)"


# ---------------------------------------------------------------------------
# JSON schema for structured outputs
# ---------------------------------------------------------------------------

_UNSUPPORTED_KEYS = {"default", "title", "minimum", "maximum", "minLength", "maxLength", "examples"}


def _strictify(node: Any) -> Any:
    if isinstance(node, dict):
        out = {k: _strictify(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYS}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"].keys())
        # pydantic emits {"$ref": ..., "description": ...}; keep only the $ref
        if "$ref" in out:
            out = {"$ref": out["$ref"]}
        return out
    if isinstance(node, list):
        return [_strictify(x) for x in node]
    return node


def extraction_json_schema() -> dict[str, Any]:
    """CardExtraction schema in the strict form required by structured outputs:
    every object has additionalProperties=false and all properties required."""
    schema = copy.deepcopy(CardExtraction.model_json_schema())
    return _strictify(schema)
