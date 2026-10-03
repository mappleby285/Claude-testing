import base64
import io

import pytest
from PIL import Image

from cardconv.duplicates import MEDIUM, STRONG, WEAK, compare, find_duplicates, needs_decision
from cardconv.imaging import ImageError, load_oriented, prepare_for_api, sha256_file, verify_image
from cardconv.schema import Contact

from .conftest import SAMPLES, extraction_dict, make_image


# ---------------------------------------------------------------- imaging
def _decode(prepared) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(prepared.data_b64)))


def test_exif_orientation_is_applied():
    img = Image.new("RGB", (400, 200), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise for display
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    oriented = load_oriented(buf.getvalue())
    assert oriented.size == (200, 400)


def test_sample_with_exif_tag_is_upright_for_api():
    data = (SAMPLES / "sample_04_exif_rotated.jpg").read_bytes()
    raw = Image.open(io.BytesIO(data))
    assert raw.width < raw.height  # stored sideways
    assert _decode(prepare_for_api(data)).width > _decode(prepare_for_api(data)).height


def test_large_image_downscaled_small_not_upscaled():
    big = prepare_for_api(make_image(size=(6000, 3500), fmt="JPEG"), max_edge=2048)
    assert max(big.width, big.height) == 2048
    small = prepare_for_api(make_image(size=(500, 300)), max_edge=2048)
    assert (small.width, small.height) == (500, 300)


def test_transparent_png_and_webp_supported():
    rgba = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    rgba.save(buf, format="PNG")
    assert _decode(prepare_for_api(buf.getvalue())).getpixel((5, 5)) == (255, 255, 255)
    webp = make_image(fmt="WEBP")
    assert prepare_for_api(webp).media_type == "image/jpeg"


def test_encoded_payload_stays_under_api_limit():
    import os
    noisy = Image.frombytes("RGB", (2048, 2048), os.urandom(2048 * 2048 * 3))
    buf = io.BytesIO()
    noisy.save(buf, format="PNG")
    prepared = prepare_for_api(buf.getvalue(), max_edge=2048)
    assert len(prepared.data_b64) <= 5 * 1024 * 1024


@pytest.mark.parametrize("data", [b"", b"not an image at all", make_image()[:200]])
def test_corrupt_images_detected(data):
    with pytest.raises(ImageError):
        verify_image(data)


def test_original_files_are_not_modified(tmp_path):
    src = tmp_path / "card.jpg"
    src.write_bytes((SAMPLES / "sample_04_exif_rotated.jpg").read_bytes())
    before = sha256_file(src)
    prepare_for_api(src.read_bytes())
    assert sha256_file(src) == before


# ------------------------------------------------------------- duplicates
def c(**kw) -> Contact:
    return Contact.model_validate(extraction_dict(**kw))


def test_same_personal_email_is_strong():
    strength, signals = compare(c(), c(full_name="J. Rivera", given_name="J.", middle_name="", phones=[]))
    assert strength == STRONG and any("same email" in s for s in signals)


def test_email_match_is_case_insensitive():
    strength, _ = compare(c(emails=["Jordan.Rivera@Northwind.example.com"]), c())
    assert strength == STRONG


def test_shared_switchboard_and_general_email_are_only_weak():
    jordan = c(emails=["info@northwind.example.com"], phones=[{"label": "main", "number": "+1 (555) 010-4477"}])
    casey = c(full_name="Casey Lin", given_name="Casey", middle_name="", family_name="Lin",
              emails=["info@northwind.example.com"], phones=[{"label": "work", "number": "555 010 4477"}])
    strength, signals = compare(casey, jordan)
    assert strength == WEAK
    assert any("general email" in s for s in signals) and any("same phone" in s for s in signals)
    assert not needs_decision([{"strength": strength}])


def test_name_and_company_is_medium_with_accents_and_suffix_variants():
    a = c(full_name="Zoë Müller", given_name="Zoë", middle_name="", family_name="Müller",
          company="Café Lumière SARL", emails=[], phones=[])
    b = c(full_name="Zoe Muller", given_name="Zoe", middle_name="", family_name="Muller",
          company="Cafe Lumiere", emails=["other@x.example"], phones=[])
    assert compare(a, b)[0] == MEDIUM


def test_name_order_insensitive_match():
    a = c(full_name="Tanaka Yuki", given_name="", middle_name="", family_name="", company="Example Robotics K.K.")
    b = c(full_name="Yuki Tanaka", given_name="Yuki", middle_name="", family_name="Tanaka",
          company="Example Robotics", emails=[], phones=[])
    assert compare(a, b)[0] == MEDIUM


def test_different_people_no_match():
    a = c()
    b = c(full_name="Lena Fischer", given_name="Lena", middle_name="", family_name="Fischer", company="Beispiel GmbH",
          emails=["l.fischer@beispiel.example"], phones=[{"label": "work", "number": "+49 30 555 0142"}])
    assert compare(a, b) == (None, [])


def test_find_duplicates_sorted_strongest_first():
    new = c()
    others = [
        ("weak", "exported", c(full_name="Casey Lin", given_name="Casey", middle_name="", family_name="Lin", emails=[],
                              phones=[{"label": "work", "number": "+1 (555) 010-4477"}])),
        ("strong", "exported", c()),
    ]
    found = find_duplicates(new, others)
    assert [f.other_id for f in found] == ["strong", "weak"]
    assert needs_decision([f.to_dict() for f in found])
