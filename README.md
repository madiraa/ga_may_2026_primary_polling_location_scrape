# GA May 2026 Primary — Advanced Polling Location Scraper

Scrapes all **advanced voting / early voting** locations for the
**May 19, 2026 Georgia General Primary Election** from the Georgia Secretary
of State's MVP (My Voter Page) portal and saves them to a CSV file.

**Source:** https://mvp.sos.ga.gov/s/advanced-voting-location-information?election=a0pcs00000J6e6HAAR&countyName=&page=advpollingplace

---

## Output

`ga_may_2026_primary_polling_locations.csv`

| Column    | Description |
|-----------|-------------|
| `county`  | Georgia county name (all-caps) |
| `name`    | Polling location name |
| `address` | Street address, City State ZIP |
| `hours`   | Hours of operation — pipe-separated, one entry per date range and event type (Advanced Polling Location or Dropbox Polling Location) |

**Stats (as of scrape date):**
- 300 total locations
- 132 counties covered

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
    sarchParam = {"election": "a0pcs00000J6e6HAAR", "countyName": ""},
    recordPerPage = 50,
    skipRecords = 0
)
```

Response structure:
```json
{
  "returnValue": {
    "totalRecords": "300",
    "ppList": "[{...}]"   // JSON-encoded string — must be double-parsed
  }
}
```

The scraper:
1. Navigates to the page with Playwright to pass Cloudflare and capture the
   dynamic `aura.context` (fwuid + app version key)
2. Uses `page.evaluate()` to call `fetch()` from inside the browser tab,
   inheriting all session cookies automatically
3. Paginates with `skipRecords` (50 records per request) until all 300
   locations are collected
4. Parses `ppList` (double-JSON), cleans `<br>` tags in addresses, and
   flattens `eventList` into pipe-separated hour strings
5. De-duplicates by Salesforce record ID and writes the CSV

---

---

## Automated Change Monitor

A GitHub Actions workflow (`monitor.yml`) runs every 4 hours and:

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

### Change log

All detected changes are appended to `change_log.json` with full before/after
values for every modified field.

---

## Files

| File | Description |
|------|-------------|
| `scrape_polling_places.py` | One-time full scraper |
| `check_for_changes.py` | Automated change monitor (run by Actions) |
| `.github/workflows/monitor.yml` | GitHub Actions schedule (every 4 hours) |
| `ga_may_2026_primary_polling_locations.csv` | Baseline CSV — auto-updated on changes |
| `change_log.json` | Append-only log of every detected change |
| `all_records_raw.json` | Full raw API response from initial scrape |
| `requirements.txt` | Python dependencies |
