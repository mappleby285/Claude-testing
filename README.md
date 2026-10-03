# Business Card → Outlook Contacts

Turn photos or scans of business cards into contact files you can import into Microsoft Outlook:

* one **vCard 3.0 (`.vcf`) file per contact**, and
* a combined **UTF-8 CSV** in two Outlook-specific layouts.

The app runs in your web browser, but only on your own computer (`http://localhost:8501`). It reads each card with an image-capable Claude model through the Anthropic API. You can then check and correct every contact before export.

> 🔒 **Privacy:** card images are **sent to Anthropic** for text extraction. Everything else stays in the app folder on your computer: the saved image copies, the database of extracted contacts, your review decisions and every export file. Nothing is sent to Microsoft. The app creates files for you to import; it never changes your Outlook address book itself.

---

## Contents

1. [Install](#1-install-one-time)
2. [API key and billing](#2-anthropic-api-key-and-billing)
3. [Start the app](#3-start-the-app)
4. [Add cards: upload](#4-add-cards-by-uploading)
5. [Add cards: folder](#5-process-a-whole-folder)
6. [Watch a folder](#6-watch-a-folder-automatic-processing)
7. [Review](#7-review-and-correct-contacts)
8. [Duplicates](#8-possible-duplicates)
9. [Export modes](#9-export-modes-review-vs-automatic)
10. [Export](#10-export)
11. [Import into Outlook](#11-import-into-outlook)
12. [Field mapping](#field-mapping)
13. [Outlook compatibility: what was checked](#outlook-compatibility-what-was-checked)
14. [Configuration](#configuration-env)
15. [Troubleshooting](#troubleshooting)
16. [Tests and project layout](#tests-and-project-layout)

---

## 1. Install (one time)

1. **Install Python 3.10 or newer** from <https://www.python.org/downloads/windows/>. On the first installer screen, tick **"Add python.exe to PATH"**, then click *Install Now*.
2. **Put this app folder** somewhere convenient, such as `C:\Users\<you>\CardConverter`. Download the ZIP and extract it, or `git clone` the repository.
3. **Double-click `setup_windows.bat`.** It will:
   * create a private Python environment in `.venv` (nothing is installed system-wide)
   * install the required packages (a few minutes)
   * create your `.env` settings file and open it in Notepad (see step 2.5 below)
   * run the automated tests. These use simulated API responses and cost nothing.

   If Windows SmartScreen warns about the file, click *More info → Run anyway*. The script only does the steps above.

## 2. Anthropic API key and billing

The app needs an Anthropic API key. **The API is billed separately from Claude.ai Pro/Max subscriptions.** A chat subscription does not include API usage.

1. Go to <https://platform.claude.com> (the Claude Console) and sign in or create an account.
2. Open the **Billing** page (under Settings) and add a payment method or buy prepaid credits. API usage is pay-as-you-go. Without credits, requests fail with a "credit balance too low" message, which the app reports clearly.
3. Optional but recommended: set a **monthly spend limit** in the Console (Limits page under Settings).
4. Open **API keys** (under Settings) and click **Create key**. Copy the key (it starts with `sk-ant-`). It is shown only once.
5. Paste it into the `.env` file after `ANTHROPIC_API_KEY=` and save:
   ```
   ANTHROPIC_API_KEY=sk-ant-...your key...
   ```
   * `.env` is listed in `.gitignore`, so it is never committed. Don't email or share it.
   * Alternatively, set a Windows user environment variable named `ANTHROPIC_API_KEY`. It takes priority over `.env`.
   * The app never prints, logs or stores your key.

**What it costs.** You pay per token (text and image size). With the default model (`claude-opus-5-5`, US$4 per million input tokens and US$20 per million output tokens at the time of writing), one card is a few thousand input tokens plus the response. Expect **roughly 3–10 US cents per card**; two-sided cards and higher `CARDCONV_EFFORT` cost more. Check current prices at <https://platform.claude.com/docs/en/about-claude/pricing>.

The **Usage** page in the Console shows your actual spend. Re-adding an image the app has already processed costs nothing: it is recognised by its content and no API call is made.

**Rate limits.** New accounts have low per-minute limits. The app processes one card at a time. If it hits a rate limit, it waits as long as the API asks and retries a limited number of times. A card that still fails is marked *Failed* and can be retried later. Other cards carry on.

## 3. Start the app

1. Double-click **`run_app.bat`**. A black console window opens, and your browser shows the app at `http://localhost:8501`.
2. The app is **bound to localhost**. Other computers on your network cannot reach it.
3. To stop the app, close the console window. Your work is saved continuously, so nothing is lost.

The sidebar shows a live **summary**: completed (exported), pending review, possible duplicates, approved but not yet exported, skipped, failed, queued, and identical images skipped. It also shows the export mode and whether an API key was found.

## 4. Add cards by uploading

1. Go to **1. Add cards → Upload images**.
2. Drag in one or more **JPG, JPEG, PNG or WebP** files. The app assumes **one card per image**.
3. **Front/back pairing:** if two images are the front and back of the *same* card, pick the back image in the front image's row ("Back side"). Files named `something_front.jpg` and `something_back.jpg` are paired automatically; you can change this. A paired card is read in a single request. If the two sides disagree, the card is flagged for review.
4. Click **Add and process**. A progress bar shows each card, followed by a summary.

What happens to each image:

* **Your original file is never modified.** The app saves a copy in `data\images\` and works on that.
* The image is turned upright according to its EXIF orientation (phone photos). Images whose longest side exceeds 2048 px are scaled down with a high-quality filter; small images are never enlarged.
* **Corrupt or unreadable files** are listed as *Failed (corrupt)* and are never sent to the API.
* **The same image added again** (even renamed) is recognised by its content hash, and no new API call is made.
* **An image with several cards** is flagged "Several cards in one image" for review. The model is told not to mix details from different cards. Re-photograph the cards one at a time for best results.

## 5. Process a whole folder

1. Go to **1. Add cards → Process a folder**.
2. Paste a folder path (for example `C:\Users\you\Pictures\Cards`) or click **Browse…**.
3. Choose whether to include subfolders and whether to pair `*_front` / `*_back` files.
4. Click **Process folder**. Images that were processed before are skipped without an API call, so running the same folder again is safe.

The same thing works from the command line, for example in a scheduled task:

```
.venv\Scripts\python -m cardconv process "C:\path\to\folder"
```

## 6. Watch a folder (automatic processing)

1. Go to **1. Add cards → Watch a folder**, choose a folder, and click **Start watching**.
2. Copy or save card images into that folder, for example from a scanner, OneDrive or your phone. While the app is running, they are processed automatically.
   * A file is processed only after it has **stopped changing for 3 seconds** and can be fully decoded, so half-copied files are never read.
   * A `*_front` file waits up to 30 seconds for its matching `*_back` file, then is processed alone.
   * Restarting the app is safe: images already in the database are recognised and not sent again.
3. Click **Stop watching** to stop. Watching also stops when you close the app.

Without the browser interface: `run_watch.bat "C:\path\to\folder"` (press Ctrl+C to stop).

## 7. Review and correct contacts

Go to **2. Review**. Pick a card from the list. The card image appears on the left and an editable form on the right.

* Fields with a problem are marked **⚠️** and show the reason in red. The reasons are explicit, never a "confidence score":
  * *Unreadable text*: the model could not read part of the card
  * *Ambiguous field assignment*: for example, a job title vs. a department, or an unlabelled phone number
  * *Name structure uncertain*: the given/family split is unclear; the full name is kept exactly as printed
  * *Front and back disagree*
  * *Several cards in one image* / *Not recognised as a business card*
  * *Invalid email format*, *Unusual phone format*, *Unusual website format*
  * *Image contains instruction-like text (ignored)*: text on the card is treated purely as data, never as instructions to the app or model
  * *No email address or phone number found*, and others
* Edit any field. Phone numbers have labels (work, mobile, fax, home, main, other). Several emails and phones are kept.
* **💾 Save edits** saves your changes without approving.
* **✅ Approve** marks the contact ready for export. If warnings remain, fix them or tick *"I have checked the flagged items"*.
* **⏭ Skip card** excludes the card from export. You can bring it back later from **All cards**.
* **🔁 Retry extraction** reads the card again (one new API call).
* Failed cards are listed at the bottom of the Review page, with the reason and a **Retry** button.

Everything is stored in a local SQLite database (`data\cards.db`), including edits, statuses and decisions. Closing the browser or restarting the PC loses nothing. A card that was mid-processing when the app closed goes back to the queue, and **1. Add cards** offers to process it.

What the extractor is told to do: transcribe only what is printed, invent nothing, leave unreadable fields blank, keep accents and non-Latin scripts, copy phone numbers exactly (with `+` prefixes and extensions, never adding a country code), and leave the given/family split blank when it is uncertain.

## 8. Possible duplicates

Each new card is compared with contacts added earlier:

| Signal | Strength | What happens |
|---|---|---|
| Same personal email address | strong | Card waits as *Possible duplicate*; you must decide |
| Same name **and** company (accents, word order and "Inc."/"GmbH" ignored) | medium | Card waits as *Possible duplicate*; you must decide |
| Same general email (`info@`, `sales@`…) | weak | Shown for information only |
| Same phone number (often a shared switchboard) | weak | Shown for information only |
| Same name, different company | weak | Shown for information only |

An **identical image** is a different case. It is detected by content hash and simply skipped, as described in step 4.

For a possible duplicate, the review page shows both contacts side by side. You can choose:

* **Keep both**: they are different people, or you want two entries
* **Skip this new card**: the new card is not exported
* **Merge selected fields**: copy the fields you tick into the existing contact. Text fields replace the old value; emails, phones and websites are added. The existing contact returns to review. Once you approve it, it is exported as a **new revision file** (`…_r2.vcf`), and the earlier file is kept.

Nothing is ever merged or overwritten automatically. Colleagues who share a company phone number or `info@` address are **not** treated as the same person.

## 9. Export modes: review vs. automatic

Choose the mode in the sidebar. The setting is saved.

* **Review (default):** every card waits for your approval.
* **Automatic:** a card is exported immediately if it passes every check: no warnings from the model, valid formats, a name, a contact method, and no possible duplicate at all. Any other card goes to the review queue.

> ⚠️ Automatic checks catch format errors and problems the model reports. They **cannot guarantee a perfect transcription**: a confidently misread digit looks valid. Spot-check automatic exports, especially phone numbers.

## 10. Export

1. Go to **3. Export** and click **📤 Export approved contacts**.
2. Files are written to the `exports\` folder:

```
exports\
  vcards\Jordan_A._Rivera_1a2b3c4d.vcf     one per contact (the ID suffix prevents name collisions)
  vcards\Jordan_A._Rivera_1a2b3c4d_r2.vcf  a later revision after an edit or merge
  csv\outlook_classic_20261003-142500.csv  this batch, classic Outlook layout
  csv\outlook_web_20261003-142500.csv      this batch, new Outlook / web layout
  csv\outlook_classic_ALL_CONTACTS.csv     every exported contact so far
  csv\outlook_web_ALL_CONTACTS.csv
```

* Every file is written **atomically**: to a temporary file first, then renamed. A crash or power cut never leaves a half-written export.
* File names keep accented letters, remove characters Windows forbids, avoid reserved names such as `CON`, and add an ID so two "John Smith" cards never overwrite each other.
* If a CSV is open in Excel, Windows locks it. The app then shows a warning instead of failing; close Excel and export again.
* The Export page also has download buttons for the CSV files.

## 11. Import into Outlook

Import **either** the CSV **or** the vCards, not both. Importing both creates duplicates. Try a small batch first.

### Classic Outlook for Windows (Microsoft 365 / Outlook 2016–2024)

**CSV (best for many contacts):**

1. **File → Open & Export → Import/Export**.
2. Choose **Import from another program or file** → *Next* → **Comma Separated Values** → *Next*.
3. Browse to `exports\csv\outlook_classic_….csv`.
4. Choose a duplicates option. Use **"Do not import duplicate items"** or **"Allow duplicates to be created"**. Avoid *"Replace duplicates"*, which overwrites existing contacts.
5. Select your **Contacts** folder → *Next*.
6. Optional: click **Map Custom Fields…** to check the mapping. The column names are Outlook's own standard names, so they map automatically.
7. Click **Finish**.

**vCard (one contact at a time):** double-click a `.vcf` file, check the contact window, then click **Save & Close**. Alternatively, in the Import/Export wizard choose **Import a VCARD file (.vcf)**. Classic Outlook imports only one contact per vCard file, which is why the app writes one file per contact.

### New Outlook for Windows, and Outlook on the web / Outlook.com

1. Open **People** (the contacts icon in the left bar).
2. Choose **Manage contacts → Import contacts** (in new Outlook this may appear directly as **Import contacts** on the ribbon).
3. Browse to `exports\csv\outlook_web_….csv` and click **Import**.

New Outlook and Outlook on the web import **CSV files only**. Microsoft's guidance is to convert vCards to CSV first, so use the CSV.

### Differences between Outlook versions

| | Classic Outlook | New Outlook / Outlook on the web |
|---|---|---|
| CSV import | Import/Export wizard, with a field-mapping dialog | People → Import contacts; no mapping dialog |
| Recommended file | `outlook_classic_*.csv` | `outlook_web_*.csv` |
| Business address, job title, department, fax | Own columns | Not in Microsoft's documented template, so placed in **Notes** |
| vCard (.vcf) import | Yes, one contact per file | No (CSV only) |
| Encoding | UTF-8 (the file includes a byte-order mark) | UTF-8 ("CSV UTF-8" is Microsoft's recommendation) |
| Header language | English headers; Microsoft says headings must match Outlook's display language | Same |

If your Outlook is set to a language other than English, the CSV headers may not be recognised. Use the classic wizard's **Map Custom Fields** to map them by hand, or import the vCards.

---

## Field mapping

| App field | vCard 3.0 | Classic CSV column | Web CSV column |
|---|---|---|---|
| Full name (as printed) | `FN` | — (see below) | — |
| Given / middle / family name | `N` (family;given;middle;prefix;suffix) | First / Middle / Last Name | First / Middle / Last Name |
| Prefix (Dr., Ms.) | `N` prefix | **Title** (Outlook's "Title" is the honorific) | Title |
| Suffix (Jr., PhD) | `N` suffix | Suffix | Suffix |
| Company / department | `ORG` (company;department) | Company / Department | Company / *Notes* |
| Job title | `TITLE` | Job Title | *Notes* |
| Emails 1–3 | `EMAIL;TYPE=INTERNET,WORK` (all kept) | E-mail Address, E-mail 2 Address, E-mail 3 Address | same |
| Emails 4+ | `EMAIL` | *Notes* | *Notes* |
| Phone: work / main | `TEL;TYPE=WORK,VOICE` | Business Phone, Business Phone 2 | Business Phone, Business Phone 2 |
| Phone: mobile | `TEL;TYPE=CELL,VOICE` | Mobile Phone | Mobile Phone |
| Phone: fax | `TEL;TYPE=WORK,FAX` | Business Fax | *Notes* |
| Phone: home | `TEL;TYPE=HOME,VOICE` | Home Phone | Home Phone |
| Phone: other | `TEL;TYPE=VOICE` | Other Phone | *Notes* |
| Extra phones (columns full) | `TEL` (all kept) | *Notes* | *Notes* |
| First website | `URL` | Web Page | Web Page |
| More websites | *Notes* | *Notes* | *Notes* |
| Business address | `ADR;TYPE=WORK` | Business Street / City / State / Postal Code / Country/Region | *Notes* |
| Other text from card | *Notes* | *Notes* | *Notes* |
| Your notes | `NOTE` | Notes | Notes |
| Reference | `UID`, plus "ref xxxxxxxx" in Notes | Notes | Notes |

**Names:** a given/family split that the extractor marked as uncertain is never forced. In the vCard, `FN` holds the name exactly as printed and the `N` parts stay empty. In the CSV, the full name goes into *First Name* unchanged, and Notes says "Name order not confirmed". Outlook then displays the name correctly, and you can split it by hand if you want.

Phone numbers are exported exactly as printed, including `+` prefixes, spaces and extensions.

## Outlook compatibility: what was checked

**Checked automatically (in the test suite):**

* Every vCard is parsed by the independent **`vobject`** library. Names, organisation, title, labelled phones, emails, URL, address and notes all round-trip, including accented and CJK text and values containing `,` `;` `\` `"` and line breaks.
* vCard format: `VERSION:3.0`, CRLF line endings only, lines folded at ≤ 75 bytes without splitting multi-byte characters, RFC 2426 escaping, UTF-8 without a byte-order mark.
* CSV: UTF-8 with a byte-order mark, CRLF rows, all fields quoted, the exact header lists, round-trip of commas, quotes and newlines in Notes, and overflow of 4+ emails and extra phones into Notes.
* Safe and unique file names, and atomic writes (an interrupted write leaves the old file intact and no temporary files behind).

**Checked against Microsoft documentation:** Microsoft's pages for *Import contacts to Outlook* and *Import contacts to Outlook.com* say:
* save the file as "CSV UTF-8"
* headings go in the first row and must be in Outlook's display language
* new Outlook and Outlook on the web import via *People → Import contacts*
* vCards should be converted to CSV for those apps

Their template lists columns such as *First Name, Middle Name, Last Name, Title, Suffix, E-mail Address … E-mail 3 Address, Business Phone, Business Phone 2, Mobile Phone, Home Phone, Company, Web Page, Notes*; the `outlook_web` profile uses only these. The `outlook_classic` profile uses the standard field names that classic Outlook's own Import/Export wizard uses for business address, job title, department and fax.

**Not tested here:** an actual import into Outlook. That needs Windows with Outlook installed. Please do this quick check with one or two contacts first.

### Manual import checklist

1. ☐ Export 2–3 test contacts, including one with accents (try the sample `sample_02_accents.png`).
2. ☐ **Classic Outlook, CSV:** import `outlook_classic_….csv` and open *Map Custom Fields*. Confirm that Job Title, Business Address and Business Fax are mapped.
3. ☐ In the imported contact, check: name, company, job title, all phone numbers in the right fields, emails, website, address, and Notes.
4. ☐ Check that accents (é, ü, å) and non-Latin text look correct, not `Ã©`.
5. ☐ **Classic Outlook, vCard:** double-click one `.vcf` and check the same fields. If accents look wrong, set `CARDCONV_VCARD_OUTLOOK_CHARSET=true` (the default). If another program rejects the files, try `false`.
6. ☐ **New Outlook / web:** import `outlook_web_….csv` and check the same fields. Job title and business address appear in Notes in this layout. Optionally try `outlook_classic_….csv` too: if your version maps those extra columns, you can use that file instead.
7. ☐ A contact whose name was left unsplit shows the full name and the note "Name order not confirmed".
8. ☐ Delete the test contacts. Then import the full file.

---

## Configuration (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Your API key (required) |
| `CARDCONV_MODEL` | `claude-opus-5-5` | Any image-capable Claude model, for example `claude-sonnet-5-5` (cheaper) |
| `CARDCONV_EFFORT` | `high` | `low` / `medium` / `high` / `xhigh` / `max`. Leave empty for models without effort support. If a model rejects the parameter, the app retries without it |
| `CARDCONV_REFUSAL_FALLBACK` | `true` | If a safety classifier declines a request, the API retries on a recommended fallback model. It is switched off automatically if a model doesn't support it |
| `CARDCONV_EXPORT_MODE` | `review` | Starting export mode (the sidebar setting then takes over) |
| `CARDCONV_MAX_IMAGE_EDGE` | `2048` | Longest side in pixels sent to the API (never upscales) |
| `CARDCONV_API_MAX_ATTEMPTS` | `3` | Attempts per card for rate limits, network and server errors (1–6) |
| `CARDCONV_API_TIMEOUT` | `120` | Seconds per request |
| `CARDCONV_VCARD_OUTLOOK_CHARSET` | `true` | Adds `CHARSET=UTF-8` to non-ASCII vCard lines for classic Outlook |
| `CARDCONV_WATCH_STABLE_SECONDS` | `3` | How long a file must be unchanged before watch mode reads it |
| `CARDCONV_WATCH_PAIR_WAIT_SECONDS` | `30` | How long a `*_front` waits for its `*_back` |
| `CARDCONV_DATA_DIR` / `CARDCONV_EXPORT_DIR` | `data` / `exports` | Storage folders |

How extraction works: the app calls the Messages API with the official `anthropic` Python SDK. It uses **structured outputs**: a JSON schema generated from the Pydantic model in `cardconv/schema.py`. The response is then validated again locally, and a malformed response counts as a failure and is retried. Details: <https://platform.claude.com/docs/en/build-with-claude/structured-outputs>.

## Troubleshooting

| Problem | Fix |
|---|---|
| "ANTHROPIC_API_KEY not set" | Edit `.env` (step 2.5), then restart `run_app.bat` |
| "The API key was rejected" | Check the key was copied completely and is active in the Console. The batch stops and cards stay queued |
| "credit balance too low" | Add credits under Console → Billing, then click *Process queued cards* |
| Many "Rate limited" failures | Retry later, or ask for a higher limit in the Console. Processing continues with other cards |
| "Model … was not found" | Check `CARDCONV_MODEL` |
| Browse… button does nothing | Paste the folder path instead (the picker window may open behind the browser) |
| Accents garbled in Outlook | Use the CSV (UTF-8 with byte-order mark), or see checklist step 5 for vCards |
| Port 8501 already in use | Close the other app window, or edit `port` in `.streamlit\config.toml` |
| Start over completely | Close the app and delete the `data` and `exports` folders. This deletes all records |

**Logs** show card reference IDs and file names only, never contact details or your API key.

## Tests and project layout

Run the tests with `run_tests.bat` or `.venv\Scripts\python -m pytest`. All API calls are mocked, so the tests are free and work offline. They cover:
* schema validation
* vCard export (verified with the independent `vobject` parser) and CSV export
* Unicode and punctuation
* duplicate detection and decisions
* restart behaviour and identical-image detection
* API failures: rate limits, network errors, server errors, malformed responses, refusals, bad keys, low credit
* watch-folder stability and pairing
* atomic writes

```
app.py                  Streamlit interface
cardconv/
  config.py             settings from .env / environment
  schema.py             Pydantic contact schema + strict JSON schema for structured outputs
  imaging.py            EXIF orientation, resizing, hashing, corrupt-file detection
  extractor.py          Anthropic API call, prompt, bounded retries, error categories
  validation.py         explicit review warnings (email format, ambiguity, ...)
  duplicates.py         duplicate signals (email / name+company / phone)
  db.py                 SQLite store (cards, images, decisions, events)
  pipeline.py           ingest → extract → validate → review → export
  vcard.py              vCard 3.0 writer (escaping, folding, CRLF)
  csv_export.py         Outlook CSV profiles
  exporter.py           safe file names, atomic writes
  watcher.py            folder processing, front/back pairing, watch mode
  __main__.py           command line (process / watch / export / status / mode)
samples/                synthetic cards with fictional details (regenerate: python tools\make_samples.py)
tests/                  automated tests
setup_windows.bat       one-time setup        run_app.bat     start the app
run_watch.bat           watch without UI      run_tests.bat   run tests
```

### Sample cards

All sample data is fictional. The domains are reserved example domains, and the phone numbers come from ranges reserved for fiction.

| File | What it exercises |
|---|---|
| `sample_01_simple.jpg` | Photo on a table, slightly rotated; work/mobile/fax phones with an extension |
| `sample_02_accents.png` | Accented French name and company, `+33` numbers |
| `sample_03_front.png` + `sample_03_back.png` | Front/back pair; Japanese name and address on the back |
| `sample_04_exif_rotated.jpg` | Stored sideways with an EXIF orientation tag; prefix, suffix and punctuation |
| `sample_05_two_cards.jpg` | Two different cards in one photo (should be flagged, not mixed) |
| `sample_06_injection.png` | Card text that tries to give instructions (should be ignored and flagged) |
| `sample_07_colleague.webp` | Colleague of sample 1: shared switchboard number and `info@` email, so only a weak duplicate signal |

### Known limitations

* The app needs an internet connection and an Anthropic API key. Cards are processed one at a time.
* Automatic validation cannot prove that every character was read correctly. Review mode is the default for that reason.
* Duplicate detection compares contacts inside this app. It does not see the contacts already in Outlook; Outlook's own duplicate options at import time cover that.
* HEIC photos (the iPhone default) are not supported. Set the iPhone camera to "Most Compatible" or export as JPEG.
