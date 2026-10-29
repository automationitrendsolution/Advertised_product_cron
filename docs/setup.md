# AdProduct server setup

A manual, step-by-step setup for a fresh Linux server (Ubuntu 22.04+ or Amazon
Linux 2023). Every command is run by hand.

The examples assume:

| What | Value |
|---|---|
| Service user | `adproduct` |
| Project directory | `/opt/adproduct` |
| uv binary | `/usr/local/bin/uv` |
| Environment file | `/opt/adproduct/.env` |

If you change these, change them in every step and in the files in `deploy/`.

---

## 1. System packages

**Ubuntu / Debian**

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git curl ca-certificates unzip
```

**Amazon Linux / RHEL**. Chromium's shared libraries come from dnf:

```bash
sudo dnf install -y python3.11 git tar gzip unzip \
    nss nspr atk at-spi2-atk cups-libs libdrm libxkbcommon \
    libXcomposite libXdamage libXfixes libXrandr mesa-libgbm \
    pango cairo alsa-lib libxshmfence
```

Sizing: headless Chromium needs about 1 GB of RAM. Network: outbound 443 to
Amazon and outbound 3306 to MySQL. No inbound ports are needed.

## 2. Service user and project directory

```bash
sudo useradd --system --create-home --home-dir /home/adproduct \
    --shell /usr/sbin/nologin adproduct

sudo mkdir -p /opt/adproduct
sudo unzip Advertised_product.zip -d /tmp/adproduct-src     # or git clone
sudo cp -r /tmp/adproduct-src/Advertised_product/. /opt/adproduct/
sudo chown -R adproduct:adproduct /opt/adproduct
```

## 3. uv, dependencies and Chromium

```bash
# uv (skip if `uv --version` already works)
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh

cd /opt/adproduct
sudo -u adproduct -H /usr/local/bin/uv sync

# Ubuntu: --with-deps also installs Chromium's system libraries
sudo -u adproduct -H /usr/local/bin/uv run playwright install --with-deps chromium
# Amazon Linux: libraries came from dnf in step 1
sudo -u adproduct -H /usr/local/bin/uv run playwright install chromium

sudo -u adproduct -H /usr/local/bin/uv run pytest      # 100 passed
```

> The browsers install under `/home/adproduct/.cache/ms-playwright`. The systemd
> unit uses `ProtectHome=read-only`, which still lets it read them.

## 4. Runtime directories

```bash
sudo -u adproduct mkdir -p /opt/adproduct/data/{downloads,processed,failed,screenshots,session} \
                           /opt/adproduct/logs
sudo chmod 700 /opt/adproduct/data/session
```

`data/session` holds the signed-in Amazon session. Treat it like a password.

## 5. Configuration (`.env`)

```bash
sudo -u adproduct cp /opt/adproduct/.env.example /opt/adproduct/.env
sudo chmod 600 /opt/adproduct/.env
sudo -u adproduct nano /opt/adproduct/.env
```

Set `AMAZON_ENTITY_ID`, the `DB_*` values, and optionally `AMAZON_EMAIL` /
`AMAZON_PASSWORD`. `.env.example` explains every variable.

## 6. Database table

`Advertised_product` already exists in `powerbidb`. Skip this step there.

For a fresh database, either set `CREATE_TABLE=true` for the first run and then
back to `false`, or create the table by hand. This DDL matches the live table:

```sql
CREATE TABLE `Advertised_product` (
  `Date` datetime DEFAULT NULL,
  `Currency` text,
  `Campaign_Name` text,
  `Attribution_type` text,
  `ASIN` text,
  `Sales` float DEFAULT NULL,
  `Units` int DEFAULT NULL,
  `id` int NOT NULL AUTO_INCREMENT,
  PRIMARY KEY (`id`),
  KEY `idx_date` (`Date`)
);
```

Every run deletes by `Date`. With more than 10 million rows, an index on `Date`
keeps that delete fast. Check that one exists with
`SHOW INDEX FROM Advertised_product`.

## 7. Amazon sign-in (once, by hand)

The scheduler cannot answer a CAPTCHA or a one-time code, so a person signs in
once. Chromium saves the session in `data/session`:

```bash
cd /opt/adproduct
sudo -u adproduct -H /usr/local/bin/uv run python run.py --login
```

On a server with no display, this runs headless and asks for the two-step code
in the terminal.

**Or** copy the session from the out-of-budget app (same Amazon account):

```bash
sudo cp -r /opt/adflow/data/session/. /opt/adproduct/data/session/
sudo chown -R adproduct:adproduct /opt/adproduct/data/session
```

## 8. Verify without writing to MySQL

```bash
sudo -u adproduct -H /usr/local/bin/uv run python run.py --period today --dry-run
```

The run should end with `status : SUCCESS`. The three files are kept in
`data/processed/` for you to inspect.

The selectors have not yet been tested against the live page. If this step fails
with exit 5, see "First live run: check the selectors" in the README.
`data/screenshots/` shows where it stopped.

## 9. Schedule it

Use **either** systemd (recommended) **or** cron, not both.

### Option A: systemd timers

The timers include `Asia/Kolkata` in the schedule, so the server's own timezone
does not matter. This needs systemd 235 or later (`systemctl --version`).

```bash
sudo cp /opt/adproduct/deploy/systemd/adproduct@.service       /etc/systemd/system/
sudo cp /opt/adproduct/deploy/systemd/adproduct-today.timer     /etc/systemd/system/
sudo cp /opt/adproduct/deploy/systemd/adproduct-yesterday.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adproduct-today.timer adproduct-yesterday.timer
```

Check them:

```bash
systemctl list-timers 'adproduct*'          # next run times (shown in server time)
sudo systemctl start adproduct@today        # one run now, by hand
journalctl -u 'adproduct@*' -f              # follow the output
```

### Option B: cron

```bash
timedatectl | grep "Time zone"              # UTC or Asia/Kolkata?
sudo cp /opt/adproduct/deploy/adproduct.cron /etc/cron.d/adproduct
sudo chmod 644 /etc/cron.d/adproduct
sudo nano /etc/cron.d/adproduct             # keep only the block for that zone
```

| Server timezone | today (every 3 h) | yesterday (14:00 IST) |
|---|---|---|
| UTC | `30 */3 * * *` | `30 8 * * *` |
| Asia/Kolkata | `0 */3 * * *` | `0 14 * * *` |

With `--quiet`, `logs/cron.log` only collects crashes that happen before logging
starts. Everything else goes to `logs/adproduct.log`.

## 10. Housekeeping (automatic)

- **Report files**: deleted once their rows are committed. Files from failed or
  dry runs are deleted after `FILE_RETENTION_DAYS`.
- **Screenshots**: deleted after `FILE_RETENTION_DAYS`.
- **`logs/adproduct.log`**: rotated at UTC midnight, kept `LOG_RETENTION_DAYS`.
- **Amazon's report list**: every run adds three reports named
  `AdProduct …`, about 27 a day. Amazon keeps them. They can be ignored.

## Rebuilding a host

Repeat steps 1 to 5, restore `.env`, then sign in again (step 7).
