"""
Deep inspection: capture the POST body and response for the polling location
data API call by selecting a county and clicking Search.
"""

import asyncio
import json
from playwright.async_api import async_playwright

URL = (
    "https://mvp.sos.ga.gov/s/advanced-voting-location-information"
    "?election=a0pcs00000J6e6HAAR&countyName=&page=advpollingplace"
)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        apex_calls = []

        async def handle_request(request):
            if "aura" in request.url and request.method == "POST":
                try:
                    body = request.post_data or ""
                    if "ApexAction" in body or "advpolling" in body.lower() or "polling" in body.lower() or "location" in body.lower():
                        apex_calls.append({
                            "type": "REQUEST",
                            "url": request.url,
                            "body": body[:2000],
                        })
                        print(f"\n[REQUEST POST] {request.url}")
                        print(body[:1000])
                except Exception as e:
                    print(f"Request capture error: {e}")

        async def handle_response(response):
            if "aura" in response.url and "ApexAction" in response.url:
                try:
                    body = await response.text()
                    apex_calls.append({
                        "type": "RESPONSE",
                        "url": response.url,
                        "body": body[:3000],
                    })
                    print(f"\n[RESPONSE] {response.url}")
                    print(body[:1500])
                except Exception as e:
                    print(f"Response capture error: {e}")

        page.on("request", handle_request)
        page.on("response", handle_response)

        print("Loading page...")
        await page.goto(URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)

        # Dump the page structure so we can find the county dropdown
        print("\n=== VISIBLE TEXT ===")
        text = await page.inner_text("body")
        print(text[:2000])

        # Try to find and interact with the county dropdown
        print("\n=== LOOKING FOR DROPDOWN ===")
        selects = await page.query_selector_all("select, lightning-combobox, .slds-select")
        print(f"Found {len(selects)} select/combobox elements")

        # Try to find any input or select
        inputs = await page.query_selector_all("input, select, button")
        for el in inputs:
            tag = await el.evaluate("el => el.tagName")
            placeholder = await el.get_attribute("placeholder") or ""
            label = await el.get_attribute("aria-label") or ""
            value = await el.get_attribute("value") or ""
            print(f"  {tag}: placeholder='{placeholder}' aria-label='{label}' value='{value}'")

        # Try clicking on a combobox / dropdown for county
        county_combo = await page.query_selector("lightning-combobox, [data-id*='county'], [placeholder*='county' i], [aria-label*='county' i]")
        if county_combo:
            print("\nFound county combobox, clicking...")
            await county_combo.click()
            await page.wait_for_timeout(1000)
            
            # Find the first option
            options = await page.query_selector_all("lightning-base-combobox-item, [role='option'], .slds-listbox__item")
            print(f"Found {len(options)} options")
            if options:
                await options[0].click()
                await page.wait_for_timeout(1000)

        # Find and click search button
        search_btn = await page.query_selector("button[type='submit'], button:has-text('Search'), lightning-button button")
        if search_btn:
            print("\nClicking search button...")
            await search_btn.click()
            await page.wait_for_timeout(5000)
        
        print("\n=== PAGE TEXT AFTER INTERACTION ===")
        text = await page.inner_text("body")
        print(text[:3000])

        with open("requests_log.json", "w") as f:
            json.dump(apex_calls, f, indent=2)
        print(f"\nCaptured {len(apex_calls)} Apex calls. Saved to requests_log.json")

        await browser.close()


asyncio.run(main())
