# AdProduct

Amazon Ads advertised-product automation. AdProduct drives the Amazon Ads
**Sponsored ads reports** page with Playwright, builds three reports, downloads
them, processes them with pandas, and loads them into the MySQL
`Advertised_product` table.

```
advertising.amazon.com/reports  (browser UI)
        ↓  Playwright + Chromium
Create report × 3, each: Daily · Today | Yesterday · Run report
   Sponsored Products  → Advertised product
   Sponsored Brands    → Attributed Purchases
   Sponsored Display   → Advertised product
        ↓
Wait for Amazon to build them → download (CSV / XLSX)
        ↓  pandas
Map columns · clean $ and commas · format dates
        ↓  SQLAlchemy, one transaction
MySQL: DELETE the dates in the data, INSERT the data  →  Advertised_product
```

**Schedule** (India time):

| Run | When | Command |
| --- | --- | --- |
| Today's reports | every 3 hours: 00:00, 03:00, … 21:00 IST | `run.py --period today` |
| Yesterday's reports | once a day at 14:00 IST | `run.py --period yesterday` |

No Amazon Ads API. Runs headless on Linux.

> ⚠️ **Amazon is retiring this page.** Its banner says: from **17 Dec 2026** new
> reports can no longer be created there, and the page is removed on
> **31 Dec 2026**. Reports move to *unified reporting*, where metric and column
> names have changed. AdProduct will stop working on 17 Dec 2026 unless it is
> moved to the new page first.

---

## Contents

- [How it works](#how-it-works)
- [Installation](#installation)
- [Configuration](#configuration)
- [Amazon session setup](#amazon-session-setup)
- [Running it](#running-it)
- [Scheduling](#scheduling)
- [The database load](#the-database-load)
- [Column mapping](#column-mapping)
- [Dates and timezones](#dates-and-timezones)
- [First live run: check the selectors](#first-live-run-check-the-selectors)
- [Troubleshooting](#troubleshooting)
- [Testing](#testing)

---

## How it works

| Module | Responsibility |
| --- | --- |
| [`app/config.py`](app/config.py) | Reads and validates every setting from the environment. |
| [`app/browser.py`](app/browser.py) | Chromium lifecycle. Persistent profile, so a signed-in session is reused. Screenshots on failure. |
| [`app/auth.py`](app/auth.py) | Fills in Amazon sign-in when the session has expired. Never answers CAPTCHA/OTP. |
| [`app/reports_page.py`](app/reports_page.py) | The reports page: **Create report**, category, report type, Daily, period, report name, **Run report**. |
| [`app/downloader.py`](app/downloader.py) | Polls the report list until each report is ready, then downloads it. |
| [`app/processor.py`](app/processor.py) | The original upload script's processing: reads CSV/XLSX, maps and cleans columns. |
| [`app/database.py`](app/database.py) | Reflects the live table; DELETE + INSERT in one transaction; ROLLBACK on failure. |
| [`app/schema.py`](app/schema.py) | The three report definitions and their column mappings. |
| [`app/main.py`](app/main.py) | Orchestration, run modes, run summary. |
| [`run.py`](run.py) | CLI. |
| [`config/selectors.json`](config/selectors.json) | Every UI locator. A changed Amazon page is a config edit, not a code change. |

A run in detail:

1. Connects to MySQL and reflects `Advertised_product` first, so a bad
   connection fails before any browser work.
2. Opens the reports page. If the saved session has expired it signs in with
   `AMAZON_EMAIL` / `AMAZON_PASSWORD`.
3. Creates the three reports, one after the other, exactly as in the manual
   steps. Each gets a **unique report name** such as
   `AdProduct SP today 20261029-083000`, which is how the run later finds its own
   report among the dozens already in the list.
4. Polls the list (every `REPORT_POLL_SECONDS`) until each report's download icon
   appears, then downloads it. Amazon builds all three at the same time.
5. Reads each file, checks its columns really are that report's (this catches a
   form left on the wrong report type), maps and cleans it.
6. Combines the three and, in one transaction, deletes every date in the data and
   inserts the new rows.
7. Deletes the downloaded files and logs a `SUCCESS`/`FAILED` summary.

If a step fails, it is retried (`MAX_RETRIES`). A retry keeps what already
worked: downloaded reports are not fetched again, and reports already requested
are waited for instead of being created a second time.

---

## Installation

```bash
uv sync                              # creates .venv and installs everything
uv run playwright install chromium   # downloads the browser
uv run pytest                        # 100 unit tests, no network
```

On a bare Linux server Chromium also needs system libraries:
`uv run playwright install --with-deps chromium` (Ubuntu/Debian). For the full
server walkthrough, see [`docs/setup.md`](docs/setup.md).

---

## Configuration

All configuration is environment variables, read from `.env` in the project
directory:

```bash
cp .env.example .env
chmod 600 .env
```

| Variable | Default | Notes |
| --- | --- | --- |
| `AMAZON_ENTITY_ID` | — | **Required.** The `entityId=…` value from the reports URL. |
| `AMAZON_EMAIL` / `AMAZON_PASSWORD` | — | Used only when the saved session has expired. Set both or neither. |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | — | **Required.** |
| `DB_TABLE` | `Advertised_product` | |
| `DB_DATE_FORMAT` | `%Y-%m-%d` | How dates are written, as the original script did. |
| `REPORT_NAME_PREFIX` | `AdProduct` | Start of every report name the run creates. |
| `REPORT_READY_TIMEOUT_SECONDS` | `1200` | How long Amazon may take to build a report. |
| `REPORT_POLL_SECONDS` | `20` | How often the list is checked while waiting. |
| `REPORT_TIMEZONE` | `US/Pacific` | The marketplace zone Amazon's Today/Yesterday follow. |
| `LOCK_WAIT_SECONDS` | `3600` | A run that finds another in progress waits this long. |
| `MAX_RETRIES` | `3` | Transient failures only. |
| `HEADLESS` | `true` | |
| `KEEP_PROCESSED_FILES` | `false` | `true` keeps each loaded file in `data/processed`. |

[`.env.example`](.env.example) documents every variable.

---

## Amazon session setup

AdProduct signs in with your own Amazon account. Chromium keeps a persistent
profile in `data/session`, so once signed in it stays signed in, and most runs
never see a login form. When the session expires, AdProduct fills in the
credentials from `.env` by itself.

If Amazon answers with a **CAPTCHA or a two-step (OTP) code**, the run stops with
exit code 3. A person answers it once:

```bash
uv run python run.py --login
```

With a display, a browser window opens. On an SSH-only server, Chromium stays
headless and the two-step code is typed into the terminal. AdProduct ticks
"Don't require code on this browser", so later scheduled runs are not asked
again.

> The out-of-budget app (AdFlow) uses the same Amazon account. Its
> `data/session` folder can be copied here instead of signing in again.

---

## Running it

| Command | What it does |
| --- | --- |
| `run.py --period today` | Production run for today. |
| `run.py --period yesterday` | Production run for yesterday. |
| `run.py --period today --headed` | Same, in a visible browser. Use it for the first live run. |
| `run.py --period today --dry-run` | Downloads and processes; **writes nothing** to MySQL. |
| `run.py --period today --download-only` | Downloads the three reports and stops. |
| `run.py --process-files A B C` | Loads three report files downloaded by hand, in any order. |
| `run.py --login` | Interactive Amazon sign-in. |

Other flags: `--log-level DEBUG`, `--env-file PATH`, `--quiet` (no console
output, for cron), `--no-lock`, `--echo-sql`.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Configuration error |
| 3 | Authentication required (needs a person: `run.py --login`) |
| 4 | Reports page did not load |
| 5 | A report setting could not be applied (see the screenshot) |
| 6 | A report failed, was not ready in time, or did not download |
| 7 | A downloaded file is not the expected report |
| 9 | Database error (rolled back, existing data intact) |
| 10 | All three reports were empty (nothing deleted or inserted) |
| 11 | Waited `LOCK_WAIT_SECONDS` for another run without getting the lock |

---

## Scheduling

Two schedules share one lock, so they never run at the same time:

- **`--period today`**, every 3 hours at 00:00, 03:00, 06:00, 09:00, 12:00, 15:00,
  18:00 and 21:00 IST.
- **`--period yesterday`**, daily at 14:00 IST. In US/Pacific that is 00:30 or
  01:30, so "Yesterday" is the complete US day.

If one run is still going when the other is due, the second run **waits** for
it instead of being skipped, so the daily run is never lost.

Ready-made schedules are in [`deploy/`](deploy/). Use one of them, not both:

- **systemd timers** (recommended): [`deploy/systemd/`](deploy/systemd/). The
  timers carry `Asia/Kolkata` in the schedule itself, so the server's timezone
  does not matter.
- **cron**: [`deploy/adproduct.cron`](deploy/adproduct.cron), with the lines for
  a server on UTC and for one on IST.

Installation steps are in [`docs/setup.md`](docs/setup.md#9-schedule-it).

---

## The database load

Your original script's load, with one change: both steps run in a **single
transaction**.

```
BEGIN
  DELETE FROM Advertised_product WHERE Date IN ('2026-10-29')   -- dates found in the new data
  INSERT … the rows of all three reports …
COMMIT                                                         -- or ROLLBACK: old rows stay
```

- **All three reports are required.** Because whole dates are replaced, loading
  only one or two reports would delete the others' rows for those dates. A run
  that cannot get all three writes nothing.
- **A DELETE is only issued alongside rows to insert.** If all three reports are
  empty, nothing is deleted (exit 10).
- **A failed insert rolls the delete back.** The table never has a date deleted
  but not replaced.
- **The live table decides the columns.** AdProduct reflects `Advertised_product`
  at startup, writes only the columns it has, and leaves `id` to the database.

Each "today" run replaces today's rows with the latest numbers. The 14:00 IST
"yesterday" run then replaces yesterday's rows with the full day's figures.

---

## Column mapping

From the original upload script, defined in [`app/schema.py`](app/schema.py):

| Table column | Sponsored Products — Advertised product | Sponsored Brands — Attributed Purchases | Sponsored Display — Advertised product |
| --- | --- | --- | --- |
| `Date` | Date | Date | Date |
| `Currency` | Currency | Currency | Currency |
| `Campaign_Name` | Campaign Name | Campaign Name | Campaign Name |
| `Attribution_type` | *(NULL)* | Attribution type | *(NULL)* |
| `ASIN` | Advertised ASIN | Purchased ASIN | Advertised ASIN |
| `Sales` | 7 Day Total Sales | 14 Day Total Sales | 14 Day Total Sales |
| `Units` | 7 Day Total Units (#) | 14 Day Total Units (#) | 14 Day Total Units (#) |

Headers are matched ignoring case and extra spaces (Amazon's own headers carry a
trailing space). `$1,204.55` becomes `1204.55` and `($45.10)` becomes `-45.10`
in every report, not only in the CSV one. Rows without a date (totals rows)
are dropped, and the drop is logged.

---

## Dates and timezones

- Amazon's **Today/Yesterday** presets follow the marketplace's zone ("Dates
  are based on the campaign's country"), which is US/Pacific for the US.
- Chromium runs in `REPORT_TIMEZONE`, so the calendar it shows agrees.
- The dates **in the downloaded data** are what get replaced. If they differ
  from the day the run expected, a warning is logged.
- The schedule is in IST. The data dates are US dates.
- Logs are timestamped in UTC. Each run's summary also prints the IST start time.

---

## First live run: check the selectors

The UI locators in [`config/selectors.json`](config/selectors.json) were built
from the step-by-step screenshots, **not yet tested against the live page**.
Before enabling the schedule, watch one run:

```bash
uv run python run.py --period today --headed --dry-run
```

If a step fails, the error names the selector key, and a screenshot of the page
is saved in `data/screenshots/`. Add a working selector at the **top** of that
key's list. You do not need to change any code. The most likely keys to need a tweak:

- `form_dropdown`: the Report category / Report type dropdowns
- `dropdown_option`: the entries in those dropdowns
- `row_download_button`: the download icon in a report's row
- `report_name_input`: the Report name box

---

## Troubleshooting

**Exit 3, `AuthenticationError`**: run `uv run python run.py --login`. The
screenshot in `data/screenshots/` shows what Amazon asked for.

**Exit 5, `ReportConfigError` naming a selector key**: Amazon changed the form.
See [First live run](#first-live-run-check-the-selectors).

**Exit 6, `Report(s) still not ready`**: Amazon was slow. Raise
`REPORT_READY_TIMEOUT_SECONDS`. **`Amazon reports … as failed`**: the run already
re-created the report and retried. If it keeps failing, create the same report by
hand to see Amazon's message.

**Exit 7, `was requested as … but its columns are those of …`**: the form
ended up on the wrong report type. Nothing was written. Check `form_dropdown`
and `dropdown_option` in a headed run.

**Exit 10**: all three reports were empty. This is normal early in the US day
for a "today" run. Nothing was deleted.

**A failed run left files behind**: they stay in `data/downloads/`, and can be
loaded without going back to Amazon:

```bash
uv run python run.py --process-files data/downloads/2026*_sp_* data/downloads/2026*_sb_* data/downloads/2026*_sd_*
```

**Logs**: `logs/adproduct.log` (rotated daily, kept `LOG_RETENTION_DAYS`).
Under systemd, the output also goes to `journalctl -u 'adproduct@*'`.

---

## Testing

```bash
uv run pytest            # 100 unit tests: no network, no MySQL (SQLite stands in)
uv run pytest --cov=app
```

The tests cover report parsing (CSV and XLSX), column mapping and cleaning,
report identification, the single-transaction DELETE + INSERT and its
rollback, the all-three-reports rule, the run lock's wait, config validation,
retry, log redaction, and selector resolution.
#   A d v e r t i s e d _ p r o d u c t _ c r o n  
 