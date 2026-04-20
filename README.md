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

## Files

| File | Description |
|------|-------------|
| `scrape_polling_places.py` | Main scraper — run this |
| `ga_may_2026_primary_polling_locations.csv` | Output CSV |
| `all_records_raw.json` | Full raw API responses (for debugging) |
| `probe_response.json` | First-page API probe response |
| `requirements.txt` | Python dependencies |
| `inspect_network.py` | Diagnostic: logs all network calls |
| `inspect_requests.py` | Diagnostic: logs POST bodies + responses |
