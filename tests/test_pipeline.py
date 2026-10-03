"""End-to-end pipeline tests: restart behaviour, identical images, review flow, duplicates,
automatic mode and failure isolation. The API client is mocked throughout."""

import vobject
import pytest

import anthropic

from cardconv import db
from cardconv.extractor import Extractor
from cardconv.pipeline import Pipeline, ReviewError

from .conftest import FakeClient, api_error, connection_error, extraction_dict, make_image, text_response

CASEY = dict(
    full_name="Casey Lin", given_name="Casey", middle_name="", family_name="Lin",
    job_title="Operations Coordinator", emails=["info@northwind.example.com"],
    phones=[{"label": "work", "number": "+1 (555) 010-4477"}],
)


def test_upload_process_review_approve_export(make_pipeline, settings):
    p, client = make_pipeline([text_response(extraction_dict())])
    added = p.add_card([(make_image("a"), "jordan.png")])
    assert added.status == "queued"
    summary = p.process_queue()
    assert summary.processed == 1 and summary.needs_review == 1  # review mode is the default
    row = p.store.get_card(added.card_id)
    assert row.status == db.NEEDS_REVIEW and row.contact.source_images == ["jordan.png"]
    p.approve(row.id)
    result = p.export_cards()
    assert result.count == 1
    card = vobject.readOne(result.vcards[0].read_text(encoding="utf-8"))
    assert card.fn.value == "Jordan A. Rivera"
    assert set(result.csvs) == {"outlook_classic", "outlook_web"}
    assert (settings.export_dir / "csv" / "outlook_classic_ALL_CONTACTS.csv").exists()
    assert p.store.get_card(row.id).status == db.EXPORTED


def test_identical_image_makes_no_second_api_call(make_pipeline):
    p, client = make_pipeline([text_response(extraction_dict())])
    image = make_image("same")
    p.add_card([(image, "a.png")])
    p.process_queue()
    again = p.add_card([(image, "renamed copy.png")])
    assert again.status == "identical"
    p.process_queue()
    assert len(client.calls) == 1
    assert p.summary()["identical_images_skipped"] == 1


def test_restart_keeps_state_edits_and_hashes(settings):
    client = FakeClient([text_response(extraction_dict())])
    p1 = Pipeline(settings, extractor=Extractor(settings, client=client, sleep=lambda s: None))
    image = make_image("restart")
    card_id = p1.add_card([(image, "card.png")]).card_id
    p1.process_queue()
    edited = p1.store.get_card(card_id).contact
    edited.job_title = "Head of Projects"
    edited.notes = "Met at the expo"
    p1.save_edits(card_id, edited)

    # "Restart": brand-new objects on the same database
    client2 = FakeClient([])
    p2 = Pipeline(settings, extractor=Extractor(settings, client=client2, sleep=lambda s: None))
    row = p2.store.get_card(card_id)
    assert row.status == db.NEEDS_REVIEW
    assert row.contact.job_title == "Head of Projects" and row.contact.notes == "Met at the expo"
    assert p2.add_card([(image, "card.png")]).status == "identical"
    assert client2.calls == []


def test_interrupted_processing_is_requeued_on_restart(settings):
    store = db.Store(settings.db_path)
    p = Pipeline(settings, store=store, extractor=Extractor(settings, client=FakeClient([]), sleep=lambda s: None))
    card_id = p.add_card([(make_image("x"), "x.png")]).card_id
    assert store.claim_card(card_id)  # simulate a crash mid-processing
    assert store.get_card(card_id).status == db.PROCESSING
    Pipeline(settings, extractor=Extractor(settings, client=FakeClient([]), sleep=lambda s: None))
    assert store.get_card(card_id).status == db.QUEUED


def test_one_failure_does_not_stop_the_batch(make_pipeline):
    p, client = make_pipeline([
        text_response(extraction_dict()),
        connection_error(), connection_error(), connection_error(),
        text_response(extraction_dict(**CASEY)),
    ])
    ids = [p.add_card([(make_image(str(i)), f"{i}.png")]).card_id for i in range(3)]
    summary = p.process_queue()
    assert summary.processed == 3 and summary.failed == 1
    statuses = [p.store.get_card(i).status for i in ids]
    assert statuses == [db.NEEDS_REVIEW, db.FAILED, db.NEEDS_REVIEW]
    failed = p.store.get_card(ids[1])
    assert failed.error_category == "network" and "3 attempts" in failed.error


def test_corrupt_image_recorded_as_failed_and_not_sent(make_pipeline):
    p, client = make_pipeline([text_response(extraction_dict())])
    bad = p.add_card([(b"\x89PNG broken", "broken.png")])
    good = p.add_card([(make_image("ok"), "ok.png")])
    assert bad.status == "corrupt"
    p.process_queue()
    assert len(client.calls) == 1
    assert p.store.get_card(bad.card_id).status == db.FAILED
    assert p.store.get_card(good.card_id).status == db.NEEDS_REVIEW
    with pytest.raises(ReviewError):
        p.retry(bad.card_id)


def test_credentials_error_stops_batch_and_keeps_cards_queued(make_pipeline):
    p, client = make_pipeline([api_error(anthropic.AuthenticationError, 401, "invalid x-api-key")])
    ids = [p.add_card([(make_image(str(i)), f"{i}.png")]).card_id for i in range(3)]
    summary = p.process_queue()
    assert "rejected" in summary.stopped_reason
    assert [p.store.get_card(i).status for i in ids] == [db.QUEUED] * 3
    assert len(client.calls) == 1


def test_failed_card_can_be_retried(make_pipeline):
    p, client = make_pipeline([api_error(anthropic.BadRequestError, 400, "bad image"), text_response(extraction_dict())])
    card_id = p.add_card([(make_image(), "a.png")]).card_id
    p.process_queue()
    assert p.store.get_card(card_id).status == db.FAILED
    assert p.retry(card_id) == db.NEEDS_REVIEW


def test_re_adding_failed_image_requeues_instead_of_skipping(make_pipeline):
    p, client = make_pipeline([api_error(anthropic.BadRequestError, 400, "bad"), text_response(extraction_dict())])
    image = make_image("retry-me")
    card_id = p.add_card([(image, "a.png")]).card_id
    p.process_queue()
    again = p.add_card([(image, "a.png")])
    assert again.status == "requeued" and again.card_id == card_id


def test_approve_requires_acknowledging_flags(make_pipeline):
    p, _ = make_pipeline([text_response(extraction_dict(emails=["jordan(at)northwind"]))])
    card_id = p.add_card([(make_image(), "a.png")]).card_id
    p.process_queue()
    with pytest.raises(ReviewError):
        p.approve(card_id)
    # Fixing the field clears the flag
    contact = p.store.get_card(card_id).contact
    contact.emails = ["jordan@northwind.example.com"]
    p.approve(card_id, contact)
    assert p.store.get_card(card_id).status == db.APPROVED


def test_acknowledged_flags_can_be_approved(make_pipeline):
    warn = [{"code": "unreadable_text", "field": "phones", "detail": "smudged"}]
    p, _ = make_pipeline([text_response(extraction_dict(warnings=warn))])
    card_id = p.add_card([(make_image(), "a.png")]).card_id
    p.process_queue()
    p.approve(card_id, acknowledge=True)
    assert p.store.get_card(card_id).status == db.APPROVED


def test_skip_card(make_pipeline):
    p, _ = make_pipeline([text_response(extraction_dict())])
    card_id = p.add_card([(make_image(), "a.png")]).card_id
    p.process_queue()
    p.skip(card_id)
    assert p.summary()["skipped"] == 1
    assert p.export_cards().count == 0


# ------------------------------------------------------------- auto mode
def test_auto_mode_exports_clean_cards_and_queues_the_rest(make_pipeline, settings):
    flagged = extraction_dict(
        full_name="Lena Fischer", given_name="Lena", middle_name="", family_name="Fischer", company="Beispiel GmbH",
        emails=["l.fischer@beispiel.example"], phones=[{"label": "work", "number": "+49 30 555 0142"}],
        warnings=[{"code": "ambiguous_field", "field": "job_title", "detail": "title or department?"}],
    )
    p, _ = make_pipeline([text_response(extraction_dict()), text_response(flagged)], mode="auto")
    a = p.add_card([(make_image("a"), "a.png")]).card_id
    b = p.add_card([(make_image("b"), "b.png")]).card_id
    summary = p.process_queue()
    assert summary.exported == 1 and summary.needs_review == 1
    assert p.store.get_card(a).status == db.EXPORTED
    assert p.store.get_card(b).status == db.NEEDS_REVIEW
    assert list((settings.export_dir / "vcards").glob("*.vcf"))
    assert list((settings.export_dir / "csv").glob("outlook_classic_2*.csv"))  # batch CSV written


def test_auto_mode_sends_possible_duplicates_to_review(make_pipeline):
    p, _ = make_pipeline([text_response(extraction_dict()), text_response(extraction_dict(**CASEY))], mode="auto")
    p.add_card([(make_image("a"), "a.png")])
    b = p.add_card([(make_image("b"), "b.png")]).card_id
    p.process_queue()
    row = p.store.get_card(b)
    assert row.status == db.NEEDS_REVIEW  # weak match (shared phone) is not auto-exported
    assert row.duplicates and row.duplicates[0]["strength"] == "weak"


# ------------------------------------------------------------- duplicates
def _two_jordans(make_pipeline, second=None):
    p, client = make_pipeline([text_response(extraction_dict()), text_response(second or extraction_dict(
        job_title="Director of Projects", phones=[{"label": "mobile", "number": "+1 555-010-7777"}]))])
    first = p.add_card([(make_image("1"), "1.png")]).card_id
    p.process_queue()
    p.approve(first)
    p.export_cards()
    second_id = p.add_card([(make_image("2"), "2.png")]).card_id
    p.process_queue()
    return p, first, second_id


def test_same_email_from_different_image_is_possible_duplicate_not_identical(make_pipeline):
    p, first, second = _two_jordans(make_pipeline)
    row = p.store.get_card(second)
    assert row.status == db.DUPLICATE_REVIEW
    assert row.duplicates[0]["other_id"] == first and row.duplicates[0]["strength"] == "strong"
    with pytest.raises(ReviewError, match="duplicate"):
        p.approve(second, acknowledge=True)


def test_keep_both(make_pipeline):
    p, first, second = _two_jordans(make_pipeline)
    p.keep_both(second)
    p.approve(second, acknowledge=True)
    assert p.export_cards().count == 1
    assert p.store.get_card(first).status == db.EXPORTED


def test_skip_new_duplicate(make_pipeline):
    p, first, second = _two_jordans(make_pipeline)
    p.skip_as_duplicate(second, first)
    row = p.store.get_card(second)
    assert row.status == db.SKIPPED and row.decision == f"duplicate_of:{first}"


def test_merge_selected_fields_creates_new_revision_without_overwriting(make_pipeline, settings):
    p, first, second = _two_jordans(make_pipeline)
    old_vcf = p.store.get_card(first).vcf_path
    p.merge_into(second, first, ["job_title", "phones"])
    merged = p.store.get_card(first)
    assert merged.contact.job_title == "Director of Projects"
    numbers = [ph.number for ph in merged.contact.phones]
    assert "+1 555-010-7777" in numbers and "+1 (555) 010-4477 ext. 214" in numbers  # added, not replaced
    assert merged.status == db.NEEDS_REVIEW  # user reviews the merged contact
    assert p.store.get_card(second).status == db.SKIPPED
    p.approve(first, acknowledge=True)
    result = p.export_cards()
    new_vcf = result.vcards[0]
    assert new_vcf.name.endswith("_r2.vcf")
    assert str(new_vcf) != old_vcf
    from pathlib import Path
    assert Path(old_vcf).exists()  # earlier export kept, never silently overwritten


def test_editing_exported_contact_creates_revision(make_pipeline):
    p, _ = make_pipeline([text_response(extraction_dict())])
    card_id = p.add_card([(make_image(), "a.png")]).card_id
    p.process_queue()
    p.approve(card_id)
    first = p.export_cards().vcards[0]
    contact = p.store.get_card(card_id).contact
    contact.job_title = "VP"
    p.save_edits(card_id, contact)
    assert p.store.get_card(card_id).status == db.NEEDS_REVIEW
    p.approve(card_id)
    second = p.export_cards().vcards[0]
    assert first.exists() and second.exists() and first != second


def test_front_back_pair_is_one_card(make_pipeline):
    p, client = make_pipeline([text_response(extraction_dict())])
    added = p.add_card([(make_image("front"), "x_front.png"), (make_image("back"), "x_back.png")])
    p.process_queue()
    row = p.store.get_card(added.card_id)
    assert row.contact.source_images == ["x_front.png", "x_back.png"]
    assert len(client.calls) == 1
    assert sum(b["type"] == "image" for b in client.calls[0]["messages"][0]["content"]) == 2


def test_no_contact_details_in_event_log(make_pipeline):
    p, _ = make_pipeline([text_response(extraction_dict())])
    card_id = p.add_card([(make_image(), "a.png")]).card_id
    p.process_queue()
    p.approve(card_id)
    p.export_cards()
    log_text = " ".join(e["message"] for e in p.store.recent_events(100))
    for secret in ("Rivera", "northwind", "555"):
        assert secret not in log_text


def test_duplicate_pair_is_flagged_once_on_the_newer_card(make_pipeline):
    p, _ = make_pipeline([text_response(extraction_dict()), text_response(extraction_dict(job_title="Director"))])
    older = p.add_card([(make_image("1"), "1.png")]).card_id
    newer = p.add_card([(make_image("2"), "2.png")]).card_id
    p.process_queue()
    assert p.store.get_card(older).status == db.NEEDS_REVIEW
    assert p.store.get_card(newer).status == db.DUPLICATE_REVIEW
    # Saving edits on the older card must not flag it against the newer one
    p.save_edits(older, p.store.get_card(older).contact)
    assert p.store.get_card(older).status == db.NEEDS_REVIEW
    assert p.store.get_card(older).duplicates == []


def test_locked_csv_does_not_break_export(make_pipeline, monkeypatch):
    import cardconv.pipeline as pipeline_module

    def locked(*args, **kwargs):
        raise PermissionError("file is open in Excel")

    p, _ = make_pipeline([text_response(extraction_dict())])
    card_id = p.add_card([(make_image(), "a.png")]).card_id
    p.process_queue()
    p.approve(card_id)
    monkeypatch.setattr(pipeline_module, "write_all_csvs", locked)
    result = p.export_cards()
    assert result.count == 1 and result.vcards[0].exists()
    assert any("ALL_CONTACTS" in w for w in result.warnings)
    assert p.store.get_card(card_id).status == db.EXPORTED


def test_stored_images_resolve_after_moving_data_folder(make_pipeline, settings, tmp_path):
    import shutil

    p, _ = make_pipeline([text_response(extraction_dict())])
    card_id = p.add_card([(make_image("move"), "a.png")]).card_id
    moved = tmp_path / "moved_data"
    shutil.copytree(settings.data_dir, moved)
    settings.data_dir = moved
    files = p.image_files(card_id)
    assert files[0][0].parent == moved / "images" and files[0][0].exists()
