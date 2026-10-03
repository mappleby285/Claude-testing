"""Streamlit browser interface. Start with run_app.bat (Windows) or:
    streamlit run app.py
The server is bound to localhost only (see .streamlit/config.toml)."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import streamlit as st

from cardconv import db
from cardconv.config import api_key_configured, load_settings
from cardconv.csv_export import PROFILE_LABELS
from cardconv.duplicates import needs_decision
from cardconv.imaging import ImageError, display_bytes
from cardconv.pipeline import MERGEABLE_LISTS, MERGEABLE_SCALARS, Pipeline, ReviewError
from cardconv.schema import Address, Contact, Phone, PhoneLabel
from cardconv.validation import blocking
from cardconv.watcher import FolderWatcher, queue_folder

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
st.set_page_config(page_title="Business Card → Outlook", page_icon="📇", layout="wide")


@st.cache_resource
def get_pipeline() -> Pipeline:
    return Pipeline(load_settings())


@st.cache_resource
def watcher_holder() -> dict:
    return {"watcher": None}


pipeline = get_pipeline()
settings = pipeline.settings

STATUS_LABELS = {
    db.QUEUED: "Queued",
    db.PROCESSING: "Processing",
    db.NEEDS_REVIEW: "Needs review",
    db.DUPLICATE_REVIEW: "Possible duplicate",
    db.APPROVED: "Approved (not exported yet)",
    db.EXPORTED: "Exported",
    db.SKIPPED: "Skipped",
    db.FAILED: "Failed",
}

FIELD_ALIASES = {
    "phone": "phones", "telephone": "phones", "fax": "phones", "mobile": "phones",
    "email": "emails", "e-mail": "emails", "website": "websites", "url": "websites", "web": "websites",
    "name": "full_name", "title": "job_title", "position": "job_title", "organization": "company",
    "street": "address", "city": "address", "region": "address", "postal_code": "address", "country": "address",
}


def field_key(raw: str) -> str:
    base = (raw or "").split("[")[0].split(".")[0].strip().lower()
    return FIELD_ALIASES.get(base, base)


def issues_by_field(issues: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for i in issues:
        out.setdefault(field_key(i.get("field", "")), []).append(i)
    return out


# --------------------------------------------------------------------- sidebar
def sidebar() -> str:
    st.sidebar.title("📇 Card → Outlook")
    page = st.sidebar.radio(
        "Go to",
        ["1. Add cards", "2. Review", "3. Export", "All cards", "Activity & settings"],
        label_visibility="collapsed",
    )
    s = pipeline.summary()
    st.sidebar.markdown("**Summary**")
    c1, c2 = st.sidebar.columns(2)
    c1.metric("Completed", s["completed"])
    c2.metric("Pending review", s["pending_review"])
    c1.metric("Duplicates", s["duplicate"])
    c2.metric("Approved", s["approved_not_exported"])
    c1.metric("Skipped", s["skipped"])
    c2.metric("Failed", s["failed"])
    c1.metric("Queued", s["queued"])
    c2.metric("Identical images", s["identical_images_skipped"], help="Re-added images recognised by content hash; no API call was made.")

    st.sidebar.divider()
    mode = pipeline.export_mode
    new_mode = st.sidebar.radio(
        "Export mode",
        ["review", "auto"],
        index=0 if mode == "review" else 1,
        format_func=lambda m: "Review (approve each card)" if m == "review" else "Automatic (export clean cards)",
        help="Automatic mode exports cards that pass all checks with no reported ambiguity and sends the rest "
        "to the review queue. Automatic checks cannot guarantee a perfect transcription.",
    )
    if new_mode != mode:
        pipeline.set_export_mode(new_mode)
        st.rerun()
    if new_mode == "auto":
        st.sidebar.caption("⚠️ Automatic checks catch format problems and reported ambiguity; they cannot guarantee every character was read correctly.")
    st.sidebar.divider()
    if api_key_configured():
        st.sidebar.success(f"API key found · model `{settings.model}`")
    else:
        st.sidebar.error("ANTHROPIC_API_KEY not set — see README step 4.")
    holder = watcher_holder()
    if holder["watcher"] and holder["watcher"].running:
        st.sidebar.info(f"👁 Watching {holder['watcher'].folder}")
    return page


def show_batch_summary(summary) -> None:
    parts = [
        f"**{summary.processed}** processed",
        f"**{summary.exported}** exported",
        f"**{summary.needs_review}** need review",
        f"**{summary.duplicates}** possible duplicates",
        f"**{summary.failed}** failed",
    ]
    st.success(" · ".join(parts))
    if summary.stopped_reason:
        st.error(f"Batch stopped: {summary.stopped_reason} Remaining cards stay queued.")
    for err in summary.errors[:20]:
        st.warning(err)


def run_queue(card_ids: list[str] | None = None) -> None:
    if not api_key_configured():
        st.error("ANTHROPIC_API_KEY is not set. Cards were saved and queued; add the key and click 'Process queued cards'.")
        return
    bar = st.progress(0.0, text="Starting…")

    def progress(i: int, total: int, _card: str) -> None:
        if total:
            bar.progress(min(i / total, 1.0), text=f"Card {min(i + 1, total)} of {total}" if i < total else "Done")

    summary = pipeline.process_queue(card_ids, progress=progress)
    bar.progress(1.0, text="Done")
    show_batch_summary(summary)


PRIVACY = (
    "🔒 **Privacy:** card images are sent to Anthropic's API for text extraction. Copies of the images, "
    "the extracted records and all exports stay on this computer (`data/` and `exports/` folders)."
)


# ------------------------------------------------------------------- add cards
def page_add() -> None:
    st.header("1. Add business cards")
    st.info(PRIVACY)
    queued = pipeline.store.list_cards((db.QUEUED,))
    if queued:
        st.warning(f"{len(queued)} card(s) are queued (for example after a restart or a stopped batch).")
        if st.button(f"▶ Process {len(queued)} queued card(s)", type="primary"):
            run_queue()

    tab_upload, tab_folder, tab_watch = st.tabs(["Upload images", "Process a folder", "Watch a folder"])

    with tab_upload:
        files = st.file_uploader(
            "JPG, JPEG, PNG or WebP. One business card per image.",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True,
        )
        if files:
            labels = [f"{i + 1}. {f.name}" for i, f in enumerate(files)]
            st.markdown(
                "**Front/back pairing** – if two images show the front and back of the *same* card, choose the back "
                "image on the front image's row. Leave empty for single-sided cards."
            )
            from cardconv.watcher import split_side

            suggested: dict[str, str] = {}
            for i, f in enumerate(files):
                base, side = split_side(Path(f.name))
                if side == "front":
                    for j, g in enumerate(files):
                        b2, s2 = split_side(Path(g.name))
                        if s2 == "back" and b2 == base:
                            suggested[labels[i]] = labels[j]
            table = pd.DataFrame({"Image": labels, "Back side (optional)": [suggested.get(l, "") for l in labels]})
            edited = st.data_editor(
                table,
                hide_index=True,
                disabled=["Image"],
                column_config={
                    "Back side (optional)": st.column_config.SelectboxColumn(options=[""] + labels, required=False)
                },
                key="pairing",
                width="stretch",
            )
            pairs = {r["Image"]: r["Back side (optional)"] for _, r in edited.iterrows() if r["Back side (optional)"]}
            problems = []
            backs = list(pairs.values())
            for front, back in pairs.items():
                if front == back:
                    problems.append(f"{front} cannot be its own back side.")
                if back in pairs:
                    problems.append(f"{back} is used as a back side and also has a back side.")
                if front in backs:
                    problems.append(f"{front} is used as a back side and also has a back side.")
            if len(set(backs)) != len(backs):
                problems.append("The same image is chosen as the back of two cards.")
            for p in problems:
                st.error(p)
            n_cards = len(files) - len(pairs)
            if st.button(f"Add and process {n_cards} card(s)", type="primary", disabled=bool(problems)):
                by_label = dict(zip(labels, files))
                used_as_back = set(backs)
                new_ids = []
                for label in labels:
                    if label in used_as_back:
                        continue
                    group = [(by_label[label].getvalue(), by_label[label].name)]
                    if label in pairs:
                        group.append((by_label[pairs[label]].getvalue(), by_label[pairs[label]].name))
                    result = pipeline.add_card(group, source="upload")
                    if result.status in ("queued", "requeued"):
                        new_ids.append(result.card_id)
                    elif result.status == "identical":
                        st.info(result.message)
                    else:
                        st.error(result.message)
                if new_ids:
                    run_queue(new_ids)

    with tab_folder:
        folder = folder_input("folder_path")
        c1, c2 = st.columns(2)
        recursive = c1.checkbox("Include subfolders", key="folder_recursive")
        pair = c2.checkbox("Pair files named *_front / *_back", value=True, key="folder_pair")
        if st.button("Process folder", type="primary", disabled=not folder):
            path = Path(folder)
            if not path.is_dir():
                st.error("Folder not found.")
            else:
                results = queue_folder(pipeline, path, recursive, pair)
                identical = [r for r in results if r.status == "identical"]
                corrupt = [r for r in results if r.status == "corrupt"]
                st.write(f"Found {len(results)} card(s): {len(identical)} already processed, {len(corrupt)} unreadable.")
                for r in corrupt:
                    st.error(r.message)
                ids = [r.card_id for r in results if r.status in ("queued", "requeued")]
                if ids:
                    run_queue(ids)
                st.caption("Original files are never modified.")

    with tab_watch:
        holder = watcher_holder()
        watcher: FolderWatcher | None = holder["watcher"]
        if watcher and watcher.running:
            st.success(f"Watching **{watcher.folder}** – new images are processed automatically while this app runs.")
            if st.button("⏹ Stop watching"):
                watcher.stop()
                st.rerun()
            watch_status()
        else:
            folder = folder_input("watch_path")
            c1, c2 = st.columns(2)
            recursive = c1.checkbox("Include subfolders", key="watch_recursive")
            pair = c2.checkbox("Pair files named *_front / *_back", value=True, key="watch_pair")
            st.caption(
                f"Files are processed once they have stopped changing for {settings.watch_stable_seconds}s "
                f"(so half-copied files are skipped). A *_front file waits up to {settings.watch_pair_wait_seconds}s "
                "for its *_back. Already-processed images are recognised by content and not sent again."
            )
            if st.button("▶ Start watching", type="primary", disabled=not folder):
                if not api_key_configured():
                    st.error("Set ANTHROPIC_API_KEY first.")
                elif not Path(folder).is_dir():
                    st.error("Folder not found.")
                else:
                    holder["watcher"] = FolderWatcher(pipeline, Path(folder), recursive, pair)
                    holder["watcher"].start()
                    st.rerun()


@st.fragment(run_every=3)
def watch_status() -> None:
    watcher = watcher_holder()["watcher"]
    if not watcher:
        return
    s = watcher.status
    c1, c2, c3 = st.columns(3)
    c1.metric("New cards", s.added)
    c2.metric("Identical (skipped)", s.identical)
    c3.metric("Files settling", s.waiting)
    if s.last_message:
        st.caption(f"Last: {s.last_message}")
    for err in s.errors[-5:]:
        st.error(err)


def folder_input(key: str) -> str:
    c1, c2 = st.columns([5, 1])
    if c2.button("Browse…", key=f"{key}_browse"):
        try:
            import tkinter
            from tkinter import filedialog

            root = tkinter.Tk()
            root.withdraw()
            root.wm_attributes("-topmost", 1)
            chosen = filedialog.askdirectory(master=root)
            root.destroy()
            if chosen:
                st.session_state[key] = chosen
        except Exception:
            st.warning("The folder picker is unavailable; paste the folder path instead.")
    return c1.text_input("Folder path", key=key, placeholder=r"C:\Users\you\Pictures\Cards").strip().strip('"')


# ---------------------------------------------------------------------- review
def show_images(card_id: str) -> None:
    for stored, name in pipeline.image_files(card_id):
        try:
            st.image(display_bytes(stored.read_bytes()), caption=name, width="stretch")
        except (OSError, ImageError):
            st.error(f"Cannot display {name}")


def bump_form() -> None:
    st.session_state["form_version"] = st.session_state.get("form_version", 0) + 1


def label_for(name: str, text: str, flagged: dict) -> str:
    return f"⚠️ {text}" if name in flagged else text


def contact_form(row: db.CardRow) -> None:
    c = row.contact
    assert c is not None
    flagged = issues_by_field(row.issues)
    # Widget keys change only after a successful action (bump_form), so text typed into
    # the form survives a rejected approval.
    key = f"{row.id}_{st.session_state.get('form_version', 0)}"
    with st.form(key=f"form_{key}"):
        def text(name: str, label: str, value: str) -> str:
            out = st.text_input(label_for(name, label, flagged), value=value, key=f"{name}_{key}")
            for i in flagged.get(name, []):
                st.markdown(f":red[{i['message']}]")
            return out

        full_name = text("full_name", "Full name (as printed)", c.full_name)
        n1, n2, n3, n4, n5 = st.columns([1, 2, 2, 2, 1])
        with n1:
            prefix = text("prefix", "Prefix", c.prefix)
        with n2:
            given = text("given_name", "Given name", c.given_name)
        with n3:
            middle = text("middle_name", "Middle name", c.middle_name)
        with n4:
            family = text("family_name", "Family name", c.family_name)
        with n5:
            suffix = text("suffix", "Suffix", c.suffix)
        o1, o2, o3 = st.columns(3)
        with o1:
            company = text("company", "Company", c.company)
        with o2:
            department = text("department", "Department", c.department)
        with o3:
            job_title = text("job_title", "Job title", c.job_title)

        emails_txt = st.text_area(
            label_for("emails", "Email addresses (one per line)", flagged), "\n".join(c.emails), key=f"emails_{key}"
        )
        for i in flagged.get("emails", []):
            st.markdown(f":red[{i['message']}]")
        st.markdown(label_for("phones", "Phone numbers", flagged))
        for i in flagged.get("phones", []):
            st.markdown(f":red[{i['message']}]")
        phones_df = st.data_editor(
            pd.DataFrame([{"label": p.label.value, "number": p.number} for p in c.phones] or [], columns=["label", "number"]),
            num_rows="dynamic",
            key=f"phones_{key}",
            width="stretch",
            column_config={
                "label": st.column_config.SelectboxColumn("Label", options=[l.value for l in PhoneLabel], required=True),
                "number": st.column_config.TextColumn("Number (as printed)"),
            },
        )
        websites_txt = st.text_area(
            label_for("websites", "Websites (one per line)", flagged), "\n".join(c.websites), key=f"web_{key}"
        )
        for i in flagged.get("websites", []):
            st.markdown(f":red[{i['message']}]")
        st.markdown(label_for("address", "Business address", flagged))
        for i in flagged.get("address", []):
            st.markdown(f":red[{i['message']}]")
        street = st.text_input("Street", c.address.street, key=f"street_{key}")
        a1, a2, a3, a4 = st.columns(4)
        city = a1.text_input("City", c.address.city, key=f"city_{key}")
        region = a2.text_input("State / province", c.address.region, key=f"region_{key}")
        postal = a3.text_input("Postal code", c.address.postal_code, key=f"postal_{key}")
        country = a4.text_input("Country", c.address.country, key=f"country_{key}")
        other_txt = st.text_area(
            "Other text from the card (kept in Notes; one item per line)", "\n".join(c.other_text), key=f"other_{key}"
        )
        notes = st.text_area("Your notes (exported to Notes)", c.notes, key=f"notes_{key}")

        general = flagged.get("", [])
        for i in general:
            st.markdown(f":red[⚠️ {i['message']}]")

        flags = blocking(row.issues)
        ack = False
        if flags:
            ack = st.checkbox(
                f"I have checked the {len(flags)} flagged item(s) against the image", key=f"ack_{key}"
            )
        b1, b2, b3 = st.columns(3)
        save = b1.form_submit_button("💾 Save edits")
        approve = b2.form_submit_button("✅ Approve", type="primary")
        skip = b3.form_submit_button("⏭ Skip card")

    if save or approve or skip:
        phones = []
        for _, r in phones_df.iterrows():
            number = str(r.get("number") or "").strip()
            if number:
                label = r.get("label") or "work"
                phones.append(Phone(label=PhoneLabel(label), number=number))
        lines = lambda s: [x.strip() for x in s.splitlines() if x.strip()]  # noqa: E731
        edited = Contact(
            cards_detected=c.cards_detected,
            full_name=full_name, prefix=prefix, given_name=given, middle_name=middle, family_name=family,
            suffix=suffix, company=company, department=department, job_title=job_title,
            emails=lines(emails_txt), phones=phones, websites=lines(websites_txt),
            address=Address(street=street, city=city, region=region, postal_code=postal, country=country),
            other_text=lines(other_txt), notes=notes, warnings=c.warnings, source_images=c.source_images,
        )
        try:
            if skip:
                pipeline.skip(row.id)
                st.toast("Card skipped")
            elif approve:
                pipeline.approve(row.id, edited, acknowledge=ack)
                st.toast("Approved – export it from the Export page")
            else:
                pipeline.save_edits(row.id, edited)
                st.toast("Saved")
            bump_form()
            st.rerun()
        except ReviewError as exc:
            # approve() saves the edits before checking, so nothing typed is lost.
            st.error(str(exc))


def duplicate_panel(row: db.CardRow) -> None:
    unresolved = [d for d in row.duplicates if not d.get("resolved")]
    if not unresolved:
        return
    must = needs_decision(unresolved)
    box = st.error if must else st.info
    box(
        ("**Possible duplicate – decide before approving.** " if must else "**Weak match with an existing contact (shown for information).** ")
        + "Different people can share a company phone number or general email address; nothing is merged automatically."
    )
    for d in unresolved:
        other = pipeline.store.get_card(d["other_id"])
        if other is None or other.contact is None:
            continue
        with st.expander(
            f"{d['strength'].upper()} match: {other.contact.display_name()} "
            f"({STATUS_LABELS.get(other.status, other.status)}) – {', '.join(d['signals'])}",
            expanded=must,
        ):
            new_c, old_c = row.contact.model_dump(), other.contact.model_dump()
            fields = list(MERGEABLE_SCALARS) + list(MERGEABLE_LISTS)
            table = pd.DataFrame(
                [{"Field": f, "Existing contact": _fmt(old_c.get(f)), "This card": _fmt(new_c.get(f))} for f in fields]
            )
            st.dataframe(table, hide_index=True, width="stretch")
            differing = [f for f in fields if _fmt(new_c.get(f)) and _fmt(new_c.get(f)) != _fmt(old_c.get(f))]
            c1, c2, c3 = st.columns(3)
            if c1.button("Keep both", key=f"keep_{row.id}_{other.id}"):
                pipeline.keep_both(row.id)
                bump_form()
                st.rerun()
            if c2.button("Skip this new card", key=f"skipdup_{row.id}_{other.id}"):
                pipeline.skip_as_duplicate(row.id, other.id)
                bump_form()
                st.rerun()
            with c3:
                chosen = st.multiselect(
                    "Fields to copy into the existing contact", differing, default=[], key=f"merge_f_{row.id}_{other.id}",
                    help="Text fields replace the existing value; email/phone/website lists are added to.",
                )
                if st.button("Merge selected fields", key=f"merge_{row.id}_{other.id}", disabled=not chosen):
                    pipeline.merge_into(row.id, other.id, chosen)
                    bump_form()
                    st.toast("Merged. The existing contact is back in review; a new revision will be exported.")
                    st.rerun()


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return ", ".join(str(v) for v in value.values() if v)
    if isinstance(value, list):
        return "; ".join(
            (f"{v.get('label')}: {v.get('number')}" if isinstance(v, dict) else str(v)) for v in value
        )
    return str(value)


def page_review() -> None:
    st.header("2. Review")
    rows = pipeline.store.list_cards(db.REVIEW_STATUSES)
    failed = pipeline.store.list_cards((db.FAILED,))
    if not rows:
        st.success("Nothing waiting for review.")
    else:
        options = {r.id: f"{STATUS_LABELS[r.status]} · {r.contact.display_name() if r.contact else '?'} · "
                         f"{len(blocking(r.issues))} flag(s) · {r.id[:8]}" for r in rows}
        default = st.session_state.get("review_card")
        ids = list(options)
        idx = ids.index(default) if default in ids else 0
        card_id = st.selectbox(f"{len(rows)} card(s) to review", ids, index=idx, format_func=options.get)
        st.session_state["review_card"] = card_id
        row = pipeline.store.get_card(card_id)
        if row and row.contact:
            left, right = st.columns([2, 3])
            with left:
                show_images(row.id)
                if st.button("🔁 Retry extraction (new API call)", key=f"retry_{row.id}"):
                    with st.spinner("Extracting…"):
                        try:
                            pipeline.retry(row.id)
                        except Exception as exc:  # credentials etc.
                            st.error(str(exc))
                        else:
                            bump_form()
                            st.rerun()
                st.caption(f"Model: {row.model} · attempts: {row.attempts}")
            with right:
                duplicate_panel(row)
                contact_form(row)

    if failed:
        st.divider()
        st.subheader(f"Failed ({len(failed)})")
        for r in failed:
            names = ", ".join(n for _, n in pipeline.store.image_paths(r.id))
            c1, c2, c3 = st.columns([4, 1, 1])
            c1.markdown(f"**{names}** – {r.error} `({r.error_category})`")
            if r.error_category != "corrupt" and c2.button("Retry", key=f"retryf_{r.id}"):
                with st.spinner("Extracting…"):
                    try:
                        pipeline.retry(r.id)
                    except Exception as exc:
                        st.error(str(exc))
                    else:
                        st.rerun()
            if c3.button("Dismiss", key=f"dismiss_{r.id}"):
                pipeline.skip(r.id, "failed card dismissed")
                st.rerun()


# ---------------------------------------------------------------------- export
def page_export() -> None:
    st.header("3. Export for Outlook")
    approved = pipeline.store.list_cards((db.APPROVED,))
    st.write(f"**{len(approved)}** approved contact(s) ready to export.")
    if approved:
        st.dataframe(
            pd.DataFrame([{"Name": r.contact.display_name(), "Company": r.contact.company,
                           "Revision": max(1, r.export_revision)} for r in approved if r.contact]),
            hide_index=True, width="stretch",
        )
    if st.button("📤 Export approved contacts", type="primary", disabled=not approved):
        result = pipeline.export_cards()
        st.success(f"Exported {result.count} contact(s) to `{settings.export_dir}`")
        for warning in result.warnings:
            st.warning(warning)
        st.session_state["last_export"] = {k: str(v) for k, v in result.csvs.items()}

    st.subheader("Files")
    st.markdown(
        f"* **vCards** (one per contact): `{settings.export_dir / 'vcards'}`\n"
        f"* **CSV** (combined): `{settings.export_dir / 'csv'}` – a file per export batch plus `*_ALL_CONTACTS.csv`"
    )
    csv_dir = settings.export_dir / "csv"
    for profile, label in PROFILE_LABELS.items():
        path = csv_dir / f"{profile}_ALL_CONTACTS.csv"
        if path.exists():
            st.download_button(
                f"Download all contacts – {label}", path.read_bytes(), file_name=path.name, mime="text/csv",
                key=f"dl_{profile}",
            )
    last = st.session_state.get("last_export")
    if last:
        for profile, p in last.items():
            path = Path(p)
            if path.exists():
                st.download_button(
                    f"Download this batch – {PROFILE_LABELS[profile]}", path.read_bytes(), file_name=path.name,
                    mime="text/csv", key=f"dlb_{profile}",
                )
    with st.expander("Which file do I import? (summary – full steps in the README)"):
        st.markdown(
            "* **Classic Outlook for Windows:** File › Open & Export › Import/Export › *Import from another program or file* "
            "› *Comma Separated Values* › choose `outlook_classic_….csv`. Or double-click a `.vcf` file and click *Save & Close* "
            "(one contact per file).\n"
            "* **New Outlook / Outlook on the web / Outlook.com:** People › *Import contacts* › choose `outlook_web_….csv`.\n"
            "* Fields with no matching Outlook column are kept in **Notes**."
        )


# ------------------------------------------------------------------- all cards
def page_all() -> None:
    st.header("All cards")
    status_filter = st.multiselect("Status", list(STATUS_LABELS), format_func=STATUS_LABELS.get)
    rows = pipeline.store.list_cards(status_filter or None)
    data = []
    for r in rows:
        data.append({
            "Ref": r.id[:8],
            "Status": STATUS_LABELS.get(r.status, r.status),
            "Name": r.contact.display_name() if r.contact else "",
            "Company": r.contact.company if r.contact else "",
            "Images": ", ".join(n for _, n in pipeline.store.image_paths(r.id)),
            "Flags": len(blocking(r.issues)),
            "Decision": r.decision,
            "Updated": r.updated_at,
        })
    st.dataframe(pd.DataFrame(data), hide_index=True, width="stretch")
    skipped = [r for r in rows if r.status == db.SKIPPED and r.contact]
    if skipped:
        choice = st.selectbox("Return a skipped card to review", [""] + [r.id for r in skipped],
                              format_func=lambda i: "" if not i else next(f"{r.contact.display_name()} ({i[:8]})" for r in skipped if r.id == i))
        if choice and st.button("Return to review"):
            pipeline.store.update_card(choice, status=db.NEEDS_REVIEW, decision="")
            st.rerun()


def page_activity() -> None:
    st.header("Activity & settings")
    st.subheader("Settings in use")
    st.markdown(
        f"* Model: `{settings.model}` · effort: `{settings.effort or 'not sent'}` · refusal fallback: `{settings.refusal_fallback}`\n"
        f"* Max image edge sent to API: {settings.max_image_edge}px · API attempts per card: {settings.api_max_attempts}\n"
        f"* Data folder: `{settings.data_dir}` · Export folder: `{settings.export_dir}`\n"
        f"* vCard CHARSET=UTF-8 hint for classic Outlook: `{settings.vcard_outlook_charset}`\n\n"
        "Change these in the `.env` file and restart the app."
    )
    st.subheader("Recent activity")
    events = pipeline.store.recent_events(100)
    st.dataframe(
        pd.DataFrame([{"Time": e["ts"], "Card": (e["card_id"] or "")[:8], "Event": e["kind"], "Detail": e["message"]} for e in events]),
        hide_index=True, width="stretch",
    )


page = sidebar()
if page.startswith("1"):
    page_add()
elif page.startswith("2"):
    page_review()
elif page.startswith("3"):
    page_export()
elif page.startswith("All"):
    page_all()
else:
    page_activity()
