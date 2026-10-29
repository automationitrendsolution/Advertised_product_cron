# Advertised_product_cron

**Automatically downloads Amazon's advertised-product reports and saves them in
the database, so nobody has to do it by hand.**

Before, someone had to log in to Amazon Ads, create three reports, wait,
download them, and run a Python script to upload them. This app does all of
that by itself, on a schedule, on our Linux server.

---

## What it does, in one picture

```
   ┌──────────────────────────┐
   │   Amazon Ads website     │   1. Opens the "Sponsored ads reports" page
   │   (Sponsored ads reports)│   2. Creates 3 reports (Daily, Today or Yesterday)
   └────────────┬─────────────┘   3. Waits until Amazon has built them
                │                 4. Downloads them
                ▼
   ┌──────────────────────────┐
   │   This app               │   5. Keeps only the columns we need
   │                          │   6. Cleans them ($21.99 → 21.99, dates, …)
   └────────────┬─────────────┘
                ▼
   ┌──────────────────────────┐
   │   MySQL database         │   7. Replaces that day's rows in
   │   powerbidb              │      the Advertised_product table
   │   Advertised_product     │
   └──────────────────────────┘
                ▼
            Power BI
```

## The three reports

| # | Report category | Report type | What we take from it |
|---|---|---|---|
| 1 | Sponsored Products | Advertised product | 7-day sales and units per ASIN |
| 2 | Sponsored Brands | Attributed Purchases | 14-day sales and units per purchased ASIN |
| 3 | Sponsored Display | Advertised product | 14-day sales and units per ASIN |

All three go into the **same table**, `Advertised_product`. See
[What ends up in the database](#what-ends-up-in-the-database).

---

## When does it run?

All times are **India time (IST)**.

| What | When | Why |
|---|---|---|
| **Today's** data | Every 3 hours: 12 AM, 3 AM, 6 AM, 9 AM, 12 PM, 3 PM, 6 PM, 9 PM | Keeps today's numbers fresh during the day |
| **Yesterday's** data | Once a day at **2 PM** | By then the US day is fully over, so this gives the final numbers for yesterday |

**How long does a run take?** About **40 minutes**. Almost all of that is
waiting for Amazon: the Sponsored Brands and Sponsored Display reports are
ready in about a minute, but Amazon takes around 40 minutes to build the
Sponsored Products report. The app itself works for only about 2 minutes.

**Two runs never happen at the same time.** If one is still going when the
next is due, the next one waits its turn. It is never skipped.

> **Dates are US dates.** Amazon's "Today" and "Yesterday" follow US (Pacific)
> time, because the campaigns are in the US. At 2 PM IST it is about 1 AM in
> the US, which is why the daily run happens then.

---

## What ends up in the database

Each report has 18–33 columns. We keep only these 7, as the old upload script
did:

| Column in the table | Example | Comes from |
|---|---|---|
| `Date` | 2026-10-29 | Date |
| `Currency` | USD | Currency |
| `Campaign_Name` | Rollershade_SP_Keywords_Broad_Medium Tail | Campaign Name |
| `Attribution_type` | Promoted | Only Sponsored Brands has this. Empty for the other two |
| `ASIN` | B08RZB4F63 | *Advertised* ASIN (SP, SD) or *Purchased* ASIN (SB) |
| `Sales` | 19.99 | **7-day** total sales (SP) or **14-day** total sales (SB, SD) |
| `Units` | 1 | **7-day** total units (SP) or **14-day** total units (SB, SD) |

**Example from 29 Oct 2026**: Amazon's files on the left, the table rows on the right:

| Report | In Amazon's file | Saved in the table |
|---|---|---|
| Sponsored Products | `Rollershade_SP_…`, ASIN `B08RZB4F63`, 7 Day Sales `19.99`, Units `1` | `2026-10-29 · USD · Rollershade_SP_… · (empty) · B08RZB4F63 · 19.99 · 1` |
| Sponsored Brands | `Oct 29, 2026`, `Snowbrush1P_SB_…`, `Promoted`, ASIN `B0BKVZ9KJY`, `$21.99`, `1` | `2026-10-29 · USD · Snowbrush1P_SB_… · Promoted · B0BKVZ9KJY · 21.99 · 1` |
| Sponsored Display | `Snowbrush1P_SD_…`, ASIN `B09DT8Z793`, 14 Day Sales `0`, Units `0` | `2026-10-29 · USD · Snowbrush1P_SD_… · (empty) · B09DT8Z793 · 0.00 · 0` |

### How the table is updated

For each date in the new data, the app **removes that date's old rows and adds
the new ones**:

- Every 3-hourly run replaces **today's** rows with the latest numbers.
- The 2 PM run replaces **yesterday's** rows with the final full-day numbers.
- Older dates are never touched.

It is safe even if something fails halfway:

- **Both steps happen together, or not at all.** If adding the new rows fails,
  the old rows come back automatically. A day's data is never left missing.
- **All three reports are needed.** If even one cannot be downloaded, nothing is
  changed. This avoids losing one report's rows.
- **Empty reports change nothing.** If Amazon returns no rows at all, the
  existing data is kept.

> **Tip:** the table has no column saying which report a row came from. You can
> tell from `Attribution_type` (filled = Sponsored Brands) or from `_SP_`,
> `_SB_`, `_SD_` in the campaign name.

---

## Is it working? How to check

**In the database**: count today's rows:

```sql
SELECT Date, COUNT(*) FROM Advertised_product
GROUP BY Date ORDER BY Date DESC LIMIT 3;
```

**In the log file**: `logs/adproduct.log`. Every run ends with a summary like
this:

```
Run summary (full (today))
  Sponsored Products Advertised product : 8633 row(s)
  Sponsored Brands Attributed Purchases : 2 row(s)
  Sponsored Display Advertised product  : 12 row(s)
  dates in data      : 2026-10-29
  Advertised_product : 8647 deleted, 8647 inserted
  status             : SUCCESS
```

`SUCCESS` means it worked. `FAILED` is followed by a line that says why.

**On the server**:

```bash
systemctl list-timers 'adproduct*'     # when the next runs are
journalctl -u 'adproduct@*' -n 50      # output of the latest runs
```

---

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `AuthenticationError` | Amazon wants someone to sign in again (usually an OTP code sent to the phone) | On the server run `uv run python run.py --login` and type the code. Do this once and runs continue normally |
| `Report(s) still not ready` | Amazon took longer than 90 minutes to build a report | Usually fixes itself on the next run. If it keeps happening, raise `REPORT_READY_TIMEOUT_SECONDS` in `.env` |
| `ReportConfigError` or `NavigationError` | Amazon changed the design of its website, so the app cannot find a button | Look at the picture in `data/screenshots/`, then ask a developer to update `config/selectors.json`. No code change is needed |
| `Database connection failed` | The database could not be reached | Check the database server is up. Nothing was changed |
| `All three reports are empty` | Amazon has no data yet (common just after midnight US time) | Nothing to do. Existing data is kept |

**A run failed after downloading?** The files stay in `data/downloads/`, so they
can be loaded without going back to Amazon. See
[Load files by hand](#load-files-by-hand).

> ⚠️ **Important date: 17 December 2026.** Amazon is shutting down the
> "Sponsored ads reports" page this app uses. From 17 Dec new reports cannot be
> created there, and on 31 Dec the page is removed. **Before then, the app must
> be moved to Amazon's new reporting**, ideally the Amazon Ads API, which would
> also make each run much faster.

---

## Commands

Run these from the project folder. On the server that is `/opt/adproduct`.

| Command | What it does |
|---|---|
| `uv run python run.py --period today` | Get today's data and save it (what the schedule runs) |
| `uv run python run.py --period yesterday` | Get yesterday's data and save it |
| `uv run python run.py --period today --dry-run` | Do everything **except** saving to the database: a safe test |
| `uv run python run.py --period today --headed` | Same, but shows the browser so you can watch (only on a PC with a screen) |
| `uv run python run.py --login` | Sign in to Amazon again (asks for the OTP code) |
| `uv run pytest` | Run the automatic tests (100 tests, about 6 seconds) |

### Load files by hand

To load three report files that were downloaded by hand, or left behind by a
failed run:

```bash
uv run python run.py --process-files  SP_file.xlsx  SB_file.csv  SD_file.xlsx
```

The order does not matter. The app recognises each report from its columns.

---

## Setting it up

### On a PC (to try it)

```bash
uv sync                               # install everything
uv run playwright install chromium    # install the browser
copy .env.example .env                # then fill in .env (see below)
uv run python run.py --login          # sign in to Amazon once
uv run python run.py --period today --headed --dry-run   # watch a test run
```

### On the Linux server

These steps assume the app lives in `/opt/adproduct` and runs as a user called
`adproduct`. Run them one at a time.

**1. Create the user and copy the app in**

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin adproduct
sudo mkdir -p /opt/adproduct
sudo unzip Advertised_product.zip -d /tmp/adp && sudo cp -r /tmp/adp/Advertised_product/. /opt/adproduct/
sudo chown -R adproduct:adproduct /opt/adproduct
```

**2. Install uv, the Python packages and the browser**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
cd /opt/adproduct
sudo -u adproduct -H uv sync
sudo -u adproduct -H uv run playwright install --with-deps chromium   # Ubuntu/Debian
sudo -u adproduct -H uv run pytest                                   # should say: 100 passed
```

On Amazon Linux, use `playwright install chromium` without `--with-deps`, and
first install Chromium's libraries with
`sudo dnf install -y nss atk at-spi2-atk cups-libs libdrm libxkbcommon libXcomposite libXdamage libXrandr mesa-libgbm pango alsa-lib`.

**3. Add the settings file**

```bash
sudo -u adproduct cp .env.example .env     # or copy your working .env from the PC
sudo chmod 600 .env
sudo -u adproduct nano .env
```

**4. Sign in to Amazon once** (it asks for the OTP code in the terminal)

```bash
sudo -u adproduct -H uv run python run.py --login
```

Or copy the `data/session` folder from a PC where you are already signed in.

**5. Test without touching the database** (takes ~40 minutes; should end with `status : SUCCESS`)

```bash
sudo -u adproduct -H uv run python run.py --period today --dry-run
```

**6. Turn on the schedule**

```bash
sudo cp deploy/systemd/adproduct@.service deploy/systemd/adproduct-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adproduct-today.timer adproduct-yesterday.timer
systemctl list-timers 'adproduct*'         # shows the next run times
```

The timers have India time built in, so the server's own timezone does not
matter. To use cron instead, see [`deploy/adproduct.cron`](deploy/adproduct.cron).
Use one or the other, not both.

### The settings file (`.env`)

All settings live in a file called `.env` in the project folder. Copy
`.env.example` to `.env` and fill it in. The important ones:

| Setting | What to put |
|---|---|
| `AMAZON_ENTITY_ID` | The `entityId=…` part of the Amazon reports page address |
| `AMAZON_EMAIL`, `AMAZON_PASSWORD` | The Amazon login, used when the saved sign-in expires |
| `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | The MySQL database details |

> 🔒 **`.env` holds passwords and `data/session/` holds the Amazon sign-in.**
> Never email them, share them, or put them in git. They are already excluded
> in `.gitignore`.

Every other setting has a sensible default and is explained in
[`.env.example`](.env.example).

---

## Files and folders

| Path | What's inside |
|---|---|
| `run.py` | The program you run |
| `app/` | The program's code |
| `config/selectors.json` | How the app finds each button on Amazon's website |
| `deploy/` | The schedules for the server (systemd timers or cron) |
| `tests/` | Automatic tests |
| `.env` | Your settings and passwords (private) |
| `data/session/` | The saved Amazon sign-in (private) |
| `data/downloads/` | Reports while they are being processed. Deleted once saved |
| `data/processed/` | Copies kept from test runs. Deleted after 30 days |
| `data/screenshots/` | Pictures of the screen when something failed. Deleted after 30 days |
| `logs/adproduct.log` | What every run did. Kept 5 days |

Every run also leaves three reports named `AdProduct SP/SB/SD …` in Amazon's
report list. That's normal and harmless.

---

## For developers

<details>
<summary>How the code is organised</summary>

| Module | Responsibility |
| --- | --- |
| [`app/config.py`](app/config.py) | Reads and validates every setting from the environment. |
| [`app/browser.py`](app/browser.py) | Chromium lifecycle. Persistent profile in `data/session`, screenshots on failure. |
| [`app/auth.py`](app/auth.py) | Fills in Amazon sign-in when the session has expired. Never answers CAPTCHA/OTP unattended. |
| [`app/reports_page.py`](app/reports_page.py) | The "New report" form: category, report type, Daily, period, unique report name, Run report. |
| [`app/downloader.py`](app/downloader.py) | Polls each report's History page until its **Download** link appears, then downloads it. |
| [`app/processor.py`](app/processor.py) | Reads CSV/XLSX, recognises the report by its columns, maps and cleans. |
| [`app/database.py`](app/database.py) | Reflects the live table; DELETE + INSERT in one transaction; ROLLBACK on failure. |
| [`app/schema.py`](app/schema.py) | The three report definitions and their column mappings. |
| [`app/main.py`](app/main.py) | Orchestration, run modes, run summary. |
| [`app/runlock.py`](app/runlock.py) | `flock` lock; a second run waits up to `LOCK_WAIT_SECONDS`. |

</details>

<details>
<summary>How a run works, step by step</summary>

1. Connects to MySQL and reads the live table's columns first, so a bad
   connection fails before any browser work.
2. Opens `advertising.amazon.com/reports?entityId=…` with the saved session,
   signing in with the `.env` credentials if it has expired.
3. For each report: **Create report** → category → report type → **Daily** →
   **Today**/**Yesterday** → a unique report name such as
   `AdProduct SP today 20261029-120819` → **Run report**. Amazon then opens that
   report's History page (`/reports/history/<id>`), whose address is remembered.
4. Reloads each History page every `REPORT_POLL_SECONDS` until its status is
   `Completed` and the Action column has a **Download** link
   (`a[href*='/download-report/']`), then downloads it.
5. Checks each file's columns really are that report's (this catches a form left
   on the wrong report type), maps and cleans it.
6. One transaction: `DELETE … WHERE Date IN (<dates in the data>)`, then INSERT.
7. Deletes the downloaded files and logs the summary.

Retries (`MAX_RETRIES`) keep what already worked: downloaded reports are not
fetched again, and requested reports are waited for, not re-created. Only a report
Amazon marks *failed* is created again.

</details>

<details>
<summary>Selectors</summary>

All UI locators are in [`config/selectors.json`](config/selectors.json), checked
against the live page on 29 Oct 2026. Each key is a list of candidates tried in
order. When Amazon changes the page, the error names the key. Add a working
selector at the **top** of that list. `{label}`, `{value}` and `{name}` are
filled in by the code.

</details>

<details>
<summary>Exit codes (for monitoring)</summary>

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Configuration error |
| 3 | Authentication required: run `run.py --login` |
| 4 | Reports page did not load |
| 5 | A report setting could not be applied (see the screenshot) |
| 6 | A report failed, was not ready in time, or did not download |
| 7 | A downloaded file is not the expected report |
| 9 | Database error (rolled back, existing data intact) |
| 10 | All three reports empty (nothing deleted or inserted) |
| 11 | Waited `LOCK_WAIT_SECONDS` for another run without getting the lock |

</details>

<details>
<summary>All settings</summary>

| Variable | Default | Notes |
| --- | --- | --- |
| `REPORT_READY_TIMEOUT_SECONDS` | `5400` | How long Amazon may take to build a report. SP took ~39 min in testing. |
| `REPORT_POLL_SECONDS` | `20` | How often a History page is checked while waiting. |
| `REPORT_TIMEZONE` | `US/Pacific` | Zone Amazon's Today/Yesterday follow; used for the browser clock and the date check. |
| `REPORT_NAME_PREFIX` | `AdProduct` | Start of every report name the run creates. |
| `LOCK_WAIT_SECONDS` | `3600` | How long a run waits for another to finish. |
| `MAX_RETRIES` | `3` | Transient failures only; sign-in problems are never retried. |
| `DB_TABLE` / `DB_DATE_COLUMN` / `DB_DATE_FORMAT` | `Advertised_product` / `Date` / `%Y-%m-%d` | |
| `KEEP_PROCESSED_FILES` | `false` | `true` keeps each loaded file in `data/processed`. |
| `HEADLESS` | `true` | `false` shows the browser. |

The full annotated list is in [`.env.example`](.env.example).

</details>

<details>
<summary>Database performance note</summary>

`Advertised_product` has ~10 million rows, and `Date` is only the second column of
the `idx_campaign_date` index, so each run's `DELETE … WHERE Date IN (…)` scans
the whole table (~28 s). An index on `Date` alone would make it near-instant:

```sql
ALTER TABLE Advertised_product ADD INDEX idx_date (`Date`);
```

</details>
