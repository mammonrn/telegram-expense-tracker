# Telegram Expense Tracker Bot

Send a photo (or PDF) of a bank transfer slip to a Telegram bot and have it:

1. Upload the original slip to Google Drive, organized as `Expenses/<Year>/<Month>/`.
2. Extract the amount, bank, date, time, sender, receiver and reference number with OCR
   (Google Cloud Vision, falling back to Tesseract).
3. Ask you to pick a category via inline buttons and optionally add a remark.
4. Save everything to a Google Sheet, with duplicate detection and an
   auto-generated Summary tab (totals, category breakdown, pie chart, monthly
   trend chart).

## Features

- Thai + English bank slip OCR, automatic bank detection (KBank, SCB, Bangkok
  Bank, Krungthai, Krungsri, TTB, GSB, PromptPay, and more).
- Tesseract-focused accuracy pipeline for the no-Vision-billing case (the
  common setup - see "OCR accuracy pipeline" below): adaptive-thresholded
  preprocessing on every OCR call, plus a two-pass targeted crop around the
  amount label specifically for slips with colored/graphic backgrounds.
- Image and PDF slip support, with automatic image compression before upload.
- Duplicate detection (by reference number, or amount+date+time) with a
  yes/no confirmation before saving a repeat.
- OCR confidence scoring; low-confidence amounts trigger a manual re-entry
  prompt instead of saving bad data.
- **Cash expenses (no slip)**: type a bare amount directly to the bot (e.g.
  "150" or "150 บาท"), or use `/cash [amount] [remark]`, to log a cash
  payment straight to the same category/remark flow — no OCR, no Drive
  upload, no duplicate check.
- **Ask for a summary directly**: type a phrase like "สรุปค่าใช้จ่าย" or
  "ค่าใช้จ่ายเดือนนี้เท่าไหร่" (or in English, "expense summary") and the bot
  replies with this month's total plus each category's amount and % share —
  no command needed, same output as `/stats_month`.
- Google Drive folder-ID caching to minimize API calls.
- Multi-user support, scoped by Telegram user ID (`ALLOWED_USER_IDS`).
- `/cash`, `/stats_month`, `/stats_year`, `/export_csv`, `/export_excel`,
  `/search_category`, `/search_date`, `/edit`, `/delete`.
- Daily automatic Google Sheet backup (Drive file copy), plus automatic
  monthly (1st of month) and yearly (Jan 1st) reports sent to each user.
- Retry with exponential backoff around Google/Telegram API calls.
- Unit tests for the parsing, dedup, and stats logic (no live credentials
  required to run them).

## Project Structure

```
main.py            # entry point, dependency wiring, job scheduling
config.py          # env-based configuration, categories, sheet schema
utils.py           # logging, retry decorator, image/date/amount helpers
auth.py            # Drive OAuth2 token loading/refreshing (see authorize_drive.py)
authorize_drive.py # one-time interactive Drive authorization script (run manually)
migrate_thai_categories.py  # one-time fix for rows saved with a Thai Category value
audit_sheet_issues.py       # read-only report of rows with Thai Category or Amount=0
ocr.py             # Vision/Tesseract OCR + Thai/English slip parsing
drive.py           # Drive folder management, caching, uploads, backups (OAuth2)
sheet.py           # Google Sheets Expenses + Summary worksheet management (service account)
database.py        # domain layer: records, dedup, search, stats, export
conversation.py    # Telegram ConversationHandler: slip -> category -> save
handlers/
  commands.py      # /start /help /stats_* /export_* /search_* /edit /delete
tests/             # pytest unit tests
deploy/
  expense-bot.service  # systemd unit
requirements.txt
.env.example
```

## Two kinds of Google credentials

This bot talks to two Google APIs with two different credential types,
because a service account cannot own files or use storage quota outside a
paid Shared Drive:

| API | Credential | Why |
|---|---|---|
| Google Sheets | Service account (`GOOGLE_APPLICATION_CREDENTIALS`) | Sheets are shared *to* the service account — no personal quota needed. |
| Google Drive | OAuth2 user account (`client_secret.json` + `token.json`) | Uploaded files need to be owned by a real Google account with Drive storage. |

## OCR accuracy pipeline

Google Cloud Vision needs billing enabled on the GCP project; without it,
`OCREngine` always falls back to Tesseract (`ocr.py`), so Tesseract's
accuracy is what actually matters day to day - especially against real
bank app slips with colored templates or decorative graphics (K PLUS /
PromptPay-style slips are the common case) rather than plain scanned
documents. Every Tesseract call goes through:

1. **General preprocessing** (`_prepare_for_ocr`): upscale small images
   (phone photos are often smaller than Tesseract wants), convert to
   grayscale, boost contrast, and binarize with an *adaptive* (local, not
   global) threshold - each pixel is compared against a blurred estimate
   of its own neighborhood rather than one fixed brightness cutoff, so a
   colored banner in one part of the slip and a photo/graphic in another
   can each binarize sensibly instead of one blowing out the other.
2. **Sparse-text page segmentation** (`--psm 11`): Tesseract's *default*
   layout analysis (PSM 3) tries to group text into paragraphs/columns,
   and a large graphic on the slip can confuse that badly enough that it
   drops an entire text block - verified directly against a synthetic
   slip where PSM 3 missed the "Amount" label and its value completely
   while PSM 11 ("find scattered text, no structure assumed") found both.
3. **Two-pass targeted amount extraction**: pass 1's word-level output
   locates the "Amount"/"จำนวนเงิน" label; pass 2 crops a region around it
   (direction - "right" or "below" the label - guided by the detected
   bank's typical layout, see `_BANK_AMOUNT_DIRECTION_HINTS`), preprocesses
   *that crop* more aggressively (bigger upscale, denoise, tighter
   threshold), and re-runs Tesseract restricted to digits only. A small,
   high-contrast, digit-only crop is a much easier problem for Tesseract
   than the whole noisy slip. The crop result must match a proper
   "X,XXX.XX" decimal format to be trusted - a crop that missed the real
   value can otherwise OCR to a stray garbage digit, which would be a
   false-positive "confident" amount worse than not guessing at all.
4. **Unchanged safety net**: if the label can't be located, or neither
   crop direction yields a valid decimal amount, everything falls through
   to the existing full-text heuristics and, ultimately, to the manual
   re-entry prompt - nothing here removes that fallback, it just needs to
   trigger less often.

None of this needs new dependencies - it's pure Pillow (`ImageOps`,
`ImageFilter`, `ImageChops`) plus `pytesseract`, both already required.

## 1. Google Cloud setup

1. Create (or reuse) a Google Cloud project at https://console.cloud.google.com.
2. Enable APIs: **Google Drive API**, **Google Sheets API**, and (optionally,
   for better OCR) **Cloud Vision API**.

### 1a. Service account (for Google Sheets)

3. Create a **Service Account**: IAM & Admin → Service Accounts → Create
   Service Account. No project roles are required — access is granted by
   sharing the Sheet directly with the service account.
4. Create a JSON key for the service account and download it. This is your
   `GOOGLE_APPLICATION_CREDENTIALS` file.
5. **Share the Google Sheet**: create a new spreadsheet, share it with the
   service account's email (found in the JSON key, e.g.
   `xxx@yyy.iam.gserviceaccount.com`) as **Editor**. Copy the spreadsheet ID
   from its URL into `SPREADSHEET_ID`. The bot creates the `Expenses` and
   `Summary` worksheets automatically on first run.
6. If Vision OCR isn't enabled/available, the bot automatically falls back
   to Tesseract — no extra Google setup is required for that path, but you
   do need Tesseract installed on the host (see below).

### 1b. OAuth Client ID (for Google Drive uploads)

7. In the same Google Cloud project, go to **APIs & Services → OAuth
   consent screen**. Choose **External**, fill in an app name/support
   email, and add your own Google account as a **test user** (this keeps
   the app in "Testing" mode, which is fine for personal use and needs no
   Google review).
8. Go to **APIs & Services → Credentials → Create Credentials → OAuth
   client ID**. Application type: **Desktop app**. Give it any name.
9. Click **Download JSON** on the client you just created. Save it as
   `client_secret.json` in the bot's project directory (path configured by
   `GOOGLE_OAUTH_CLIENT_SECRET_PATH`, default `./client_secret.json`).
10. Decide which Google Drive should receive the uploaded slips — this is
    the personal account you'll authorize in step 3 below. If you want a
    specific parent folder rather than the account's Drive root, create it
    and copy its folder ID from the URL into `GOOGLE_DRIVE_FOLDER_ID`.

## 2. Telegram Bot setup

1. Talk to [@BotFather](https://t.me/BotFather), run `/newbot`, and copy the
   token into `BOT_TOKEN`.
2. Get your numeric Telegram user ID (e.g. via [@userinfobot](https://t.me/userinfobot))
   and put it in `ALLOWED_USER_IDS` (comma-separated for multiple users).
   Leave empty to allow anyone to use the bot.

## 3. Local installation

```bash
git clone https://github.com/mammonrn/telegram-expense-tracker.git
cd telegram-expense-tracker
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# System dependency for the Tesseract OCR fallback:
#   Ubuntu/Debian: sudo apt-get install tesseract-ocr tesseract-ocr-tha poppler-utils
#   (poppler-utils provides pdftoppm, needed by pdf2image for PDF slips)

cp .env.example .env
# edit .env: BOT_TOKEN, GOOGLE_APPLICATION_CREDENTIALS, SPREADSHEET_ID,
# GOOGLE_OAUTH_CLIENT_SECRET_PATH, GOOGLE_DRIVE_FOLDER_ID, ALLOWED_USER_IDS
# place service_account.json and client_secret.json next to main.py

python authorize_drive.py   # one-time Drive authorization - see below
python main.py
```

## 4. One-time Drive authorization (headless VPS friendly)

Google Drive access can't be pre-baked into a JSON key file like the
service account — a real person has to grant consent once. Since this bot
typically runs on a VPS over SSH with no browser, `authorize_drive.py`
uses a manual copy/paste flow instead of the usual "open a browser on this
machine" pattern:

```bash
python authorize_drive.py
```

1. The script prints a Google sign-in URL.
2. Copy that URL and open it in a browser on **any device** — your phone,
   your laptop, it doesn't need to be the VPS.
3. Sign in with the Google account you want the bot to upload slips to,
   and click **Allow**.
4. The browser will try to redirect to `http://localhost/...` and fail to
   load (e.g. "This site can't be reached") — **that's expected**, since
   nothing is running on your personal device. The authorization code is
   in that URL regardless of whether the page loaded.
5. Copy the full URL from the address bar (or just the `code=...` value)
   and paste it back into the terminal prompt.
6. The script saves a token to `token.json` (path configured by
   `GOOGLE_OAUTH_TOKEN_PATH`). The bot reuses and auto-refreshes this token
   on every subsequent start — you only need to repeat this if the token is
   revoked or deleted.

If the bot ever logs a `DriveAuthError` telling you to re-authorize, just
run `python authorize_drive.py` again.

## 5. Running tests

```bash
pip install pytest
pytest
```

Tests cover amount/date parsing, Thai/English slip OCR parsing, the OAuth
code-extraction helper, and the duplicate-detection/search/stats logic
against an in-memory fake sheet — no live Google or Telegram credentials
needed.

## 6. Deploy on Ubuntu 24.04 (systemd)

```bash
sudo useradd --system --home /opt/expense_bot --shell /usr/sbin/nologin expensebot
sudo mkdir -p /opt/expense_bot /var/log/expense_bot
sudo chown -R expensebot:expensebot /opt/expense_bot /var/log/expense_bot

# Copy the project (including .env, service_account.json, and
# client_secret.json) to /opt/expense_bot, then as the expensebot user:
sudo -u expensebot python3.12 -m venv /opt/expense_bot/venv
sudo -u expensebot /opt/expense_bot/venv/bin/pip install -r /opt/expense_bot/requirements.txt

# One-time interactive Drive authorization (do this BEFORE enabling the
# systemd service, since it needs a human at the terminal):
sudo -u expensebot /opt/expense_bot/venv/bin/python /opt/expense_bot/authorize_drive.py

sudo cp /opt/expense_bot/deploy/expense-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now expense-bot
sudo systemctl status expense-bot
journalctl -u expense-bot -f
```

The unit sets `Restart=always` so the bot auto-restarts on crash or reboot.
`token.json` is created once by `authorize_drive.py` and reused/refreshed
automatically by the running service afterwards.

## 7. Backups

In addition to the daily automatic Google Sheet backup (a dated copy in
Drive, configured via `DAILY_BACKUP_HOUR_UTC` / `GOOGLE_DRIVE_BACKUP_FOLDER_ID`),
it's good practice to also periodically back up:

- The `.env` file, `service_account.json`, `client_secret.json`, and
  `token.json` (store securely, never in git — `token.json` alone grants
  Drive access to whichever account authorized it).
- `.drive_folder_cache.json` (safe to delete — it will be rebuilt from Drive
  on next use, just costs a few extra API calls).

## Conversation Flow

All bot replies are in Thai (see [User-facing language](#user-facing-language) below).

```
User sends slip photo/PDF
  -> bot uploads original to Drive, runs OCR
  -> if amount unclear: "🤔 อ่านจำนวนเงินไม่ชัดเจนครับ กรุณาพิมพ์จำนวนเงินที่ถูกต้อง"
  -> if duplicate found: "⚠️ สลิปนี้มีการบันทึกไว้แล้วครับ ต้องการบันทึกซ้ำอีกครั้งไหม?" (ใช่ / ไม่)
  -> bot shows extracted info ("พบข้อมูลดังนี้ครับ...") + category buttons
  -> bot asks "ต้องการเพิ่มหมายเหตุไหม?" (ข้าม / พิมพ์หมายเหตุ)
  -> bot saves to Google Sheets and replies "✅ บันทึกรายการเรียบร้อยแล้ว"
```

Cash expense (no slip) — skips OCR, Drive upload, and duplicate check:

```
User types "150" (or "/cash 150 ค่ากาแฟ") directly to the bot
  -> bot shows category buttons
  -> bot asks "ต้องการเพิ่มหมายเหตุไหม?" (ข้าม / พิมพ์หมายเหตุ), unless a remark was
     already given as part of "/cash <amount> <remark>"
  -> bot saves to Google Sheets and replies "✅ บันทึกรายการเรียบร้อยแล้ว"
```

Instant expense summary — no command needed:

```
User types "สรุปค่าใช้จ่าย" / "ค่าใช้จ่ายเดือนนี้เท่าไหร่" / "expense summary"
  -> bot replies with this month's total plus each category's
     amount and % share of the total (same as /stats_month)
```

## Categories

🍜 Food, 🏨 Accommodation, 🚗 Transportation, 🎮 Entertainment, 🍺 Alcohol,
📚 Education, 🙏 Donation, 🛍 Shopping, 💡 Bills, 🏥 Healthcare,
📈 Investment, 👨‍👩‍👧 Family, 💼 Business, 📦 Other.

These English names are the **canonical values** (`config.py`'s
`CATEGORIES`) - they're what actually gets written into the Sheet's
Category column, and what search/stats aggregation match against. They
must never change once records exist, or old and new rows silently stop
aggregating together (see "Migrating Thai category values" below for
what happens when this rule is broken).

For display, each category has a Thai translation in
`config.py`'s `CATEGORY_LABELS_TH` (🍜 อาหาร, 🏨 ที่พัก, 🚗 การเดินทาง,
🎮 ความบันเทิง, 🍺 เหล้า, 📚 การศึกษา, 🙏 บริจาค, 🛍 ช้อปปิ้ง, 💡 ค่าบิล,
🏥 สุขภาพ, 📈 การลงทุน, 👨‍👩‍👧 ครอบครัว, 💼 ธุรกิจ, 📦 อื่นๆ) rendered via
`category_display()` at the point a message is sent - never stored.

## User-facing language

Every message the bot sends to Telegram (prompts, buttons, confirmations,
error messages, category labels, scheduled reports) is in Thai, via
display-time translation (`category_display()` for categories; plain
Thai strings everywhere else). **The data written to the Google Sheet is
always the English canonical value** - Category, like every other
column, is storage-first: display language must never leak into what
gets saved, searched, or aggregated.

Logging (`logger.info`/`logger.error`, for developer debugging) stays in
English, as does all code — comments, variable/function names, and the
CLI prompts in `authorize_drive.py` / `migrate_thai_categories.py`
(admin setup/maintenance tools, not part of the bot's chat UI). The
`/edit` flow's field names (`Amount`, `Category`, `Remark`, `Bank`,
`Sender`, `Receiver`) are also kept in English since they must match the
Google Sheet's column headers exactly for the edit to work; the
surrounding prompts around them are in Thai. Typing a Thai category name
into `/search_category` or `/edit`'s Category field is normalized back
to the English canonical value automatically (`CATEGORY_LABELS_EN`)
before it's used to query or save anything.

## Migrating Thai category values (one-time fix)

An earlier version of the Thai translation briefly stored the *Thai
display label* directly in the Category column instead of the English
canonical value (e.g. a row saved as "อาหาร" instead of "Food"). Rows
saved during that window silently stopped aggregating with older/newer
rows in the same category, since `/stats_month`, `/stats_year`, and the
Summary sheet's `SUMIF` formulas all match on the exact stored string.

If your Sheet has any rows from that window, run this once to fix them:

```bash
python migrate_thai_categories.py --dry-run   # preview: lists affected rows, changes nothing
python migrate_thai_categories.py              # applies the fix
```

It scans every row, finds any Category value matching a known Thai
label, and rewrites it to the matching English canonical value (e.g.
"อาหาร" -> "Food"). Rows already in English, or containing an
unrecognized/custom category, are left untouched. Safe to run more than
once - a second run will report nothing left to migrate.

A related bug also let OCR failures silently save `Amount = 0` instead of
prompting for manual re-entry (fixed - see `is_amount_valid()` in
`utils.py`). There's no way to recover the real amount for an affected
row automatically, so it's not part of the auto-migration. To find any
existing rows with either problem without changing anything:

```bash
python audit_sheet_issues.py
```

This is read-only - it prints the affected row numbers so you can fix
them yourself, e.g. through `/edit` in the bot. For an Amount = 0 row,
that row's Drive URL / Telegram File ID column points at the original
slip image, so you can look up the real amount there.

## Notes on the Sheet schema

The `Expenses` worksheet has one extra column beyond the original spec —
**User ID** — appended at the end. It's required to scope multi-user
search/stats/export correctly and is additive, so it doesn't break any
formula or column expecting the original layout.
