# GA November 2026 General & Special Elections — Advanced Polling Location Scraper

Scrapes all **advanced voting / early voting** locations for the
**November 3, 2026 Georgia General & Special Elections** from the Georgia
Secretary of State's MVP (My Voter Page) portal and saves them to a CSV file.

**Source:** https://mvp.sos.ga.gov/s/advanced-voting-location-information?election=a0pcs00000J6eJBAAZ&countyName=&page=advpollingplace

> This repo previously scraped the May 19, 2026 primary. Those files
> (`ga_may_2026_primary_polling_locations.csv`, `change_log.json`,
> `check_log.txt`) are kept as a historical archive and are no longer updated.

---

## Output

`ga_nov_2026_general_polling_locations.csv`

| Column    | Description |
|-----------|-------------|
| `polling_place_county`  | Georgia county name (all-caps) |
| `polling_place_name`    | Polling location name |
| `polling_place_address_full` | Street address, City State ZIP |
| `hours_advanced_polling`   | Hours of operation — pipe-separated, one entry per date range and event type (Advanced Polling Location or Dropbox Polling Location) |

**Stats (as of scrape date):**
- 323 total locations
- 144 counties covered

---

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

---

## Running the Scraper

```bash
source venv/bin/activate
python scrape_polling_places.py
```

The browser will open visibly — this is intentional. The site uses Cloudflare
bot protection, so the scraper uses a real browser (Playwright/Chromium) to
load the page and authenticate, then makes the Salesforce Aura API calls from
within that authenticated browser context.

**Do not run with a VPN** — VPN IPs often fail Cloudflare's bot checks.

---

## How It Works

The GA MVP portal is built on Salesforce Experience Cloud (formerly Community
Cloud). All data loads via the **Salesforce Aura API** at:

```
POST https://mvp.sos.ga.gov/s/sfsites/aura?r=N&aura.ApexAction.execute=1
```

The key Apex controller method is:
```
vrWebIntegrationController.getAdvPollingPlaces(
    sarchParam = {"election": "a0pcs00000J6eJBAAZ", "countyName": ""},
    recordPerPage = 50,
    skipRecords = 0
)
```

Response structure:
```json
{
  "returnValue": {
    "totalRecords": "337",
    "ppList": "[{...}]"   // JSON-encoded string — must be double-parsed
  }
}
```

The scraper:
1. Navigates to the page with Playwright to pass Cloudflare and capture the
   dynamic `aura.context` (fwuid + app version key)
2. Uses `page.evaluate()` to call `fetch()` from inside the browser tab,
   inheriting all session cookies automatically
3. Paginates with `skipRecords` (50 records per request), passing
   `countyName: ""` — this returns **every county in one query**, it does not
   loop over the county dropdown
4. Parses `ppList` (double-JSON), cleans `<br>` tags in addresses, and
   flattens `eventList` into pipe-separated hour strings
5. De-duplicates by Salesforce record ID and writes the CSV

---

## Automated Change Monitor

A GitHub Actions workflow (`monitor.yml`) runs **daily at 11:00 UTC (7:00 AM
ET)** and:

1. Scrapes the current data from the SOS portal
2. Compares it against the stored baseline CSV
3. If anything changed — sends an email and updates the log
4. Commits the updated CSV + log back to the repo automatically

### What triggers a notification

| Change type | Example |
|-------------|---------|
| New location added | A new early voting site opens |
| Location removed | A site is cancelled |
| Address changed | A location moves |
| Hours changed | Operating hours are extended/reduced |

### Setting up email notifications (one-time)

The workflow reads three **GitHub repository secrets**. Add them at:  
`https://github.com/madiraa/ga_may_2026_primary_polling_location_scrape/settings/secrets/actions`

| Secret name | Value |
|-------------|-------|
| `EMAIL_SENDER` | Gmail address the alerts will come **from** |
| `EMAIL_APP_PASSWORD` | Gmail **App Password** (not your login password) |
| `EMAIL_RECIPIENT` | Address(es) to receive alerts — comma-separated for multiple |

These were already set up for the primary monitor and don't need to change.

**Getting a Gmail App Password:**
1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Under "How you sign in to Google" → enable **2-Step Verification** if not already on
3. Search for "App passwords" → create one named `GA Polling Monitor`
4. Copy the 16-character password → paste as `EMAIL_APP_PASSWORD`

### Running the monitor manually

To trigger it immediately without waiting for the schedule:  
Go to **Actions → GA Polling Location Monitor → Run workflow** in GitHub.

Or locally:

```bash
source venv/bin/activate
EMAIL_SENDER=you@gmail.com \
EMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx \
EMAIL_RECIPIENT=you@gmail.com \
python check_for_changes.py
```

### Google Sheets auto-sync

When changes are detected, the monitor also pushes them to the tracking sheet automatically:

**Sheet:** [2026 GA EV General Election Locations](https://docs.google.com/spreadsheets/d/1MPKVyxdFGWwQt2-uXDTQ6LTGa6UPH61tTrab1D4cFCs)

| Change type | What happens in the sheet |
|-------------|--------------------------|
| New location | New row appended (columns A–J filled; K–L left blank for geocoding) |
| Address changed | Column D updated in the existing row |
| Hours changed | Column J updated in the existing row |
| Removed location | Row is **kept** in the sheet — flagged in email + tagged `REMOVED` in column M |

The sheet starts blank — `update_sheet.py` writes the header row automatically
on first run.

**One-time setup (Google Service Account):**

This repo reuses the **same service account** already set up for the May
primary monitor (the `GOOGLE_CREDENTIALS` GitHub secret doesn't need to change).
The only new step:

1. Find the service account's email (ends in `@...iam.gserviceaccount.com`) —
   it's in the JSON key file you downloaded when you first set this up, or
   under **IAM & Admin → Service Accounts** in the GCP project you created it in.
2. Open the [new sheet](https://docs.google.com/spreadsheets/d/1MPKVyxdFGWwQt2-uXDTQ6LTGa6UPH61tTrab1D4cFCs)
   and **share it with that email as Editor**.

If you're setting this up fresh instead (no existing service account):

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project (or use an existing one) → **APIs & Services → Enable APIs**
3. Enable: **Google Sheets API** and **Google Drive API**
4. Go to **IAM & Admin → Service Accounts → Create Service Account**
5. Name it `ga-polling-monitor`, click Create
6. Click the service account → **Keys → Add Key → JSON** → download the file
7. **Share the spreadsheet** with the service account email (ends in `@...iam.gserviceaccount.com`) as **Editor**
8. Add a GitHub secret named `GOOGLE_CREDENTIALS` — paste the **entire contents** of the downloaded JSON file as the value

To run a one-time full sync from your local machine:
```bash
source venv/bin/activate
GOOGLE_CREDENTIALS=$(cat your-service-account.json) python update_sheet.py
```

### Change log

All detected changes are appended to `change_log_nov2026.json` with full
before/after values for every modified field.

---

## Files

| File | Description |
|------|-------------|
| `scrape_polling_places.py` | One-time full scraper |
| `check_for_changes.py` | Automated change monitor (run by Actions) |
| `update_sheet.py` | Google Sheets sync helper |
| `.github/workflows/monitor.yml` | GitHub Actions schedule (daily) |
| `ga_nov_2026_general_polling_locations.csv` | Baseline CSV — auto-updated on changes |
| `change_log_nov2026.json` | Append-only log of every detected change |
| `all_records_raw.json` | Full raw API response from initial scrape |
| `requirements.txt` | Python dependencies |
| `ga_may_2026_primary_polling_locations.csv`, `change_log.json`, `check_log.txt` | Historical archive from the May primary monitor — no longer updated |
