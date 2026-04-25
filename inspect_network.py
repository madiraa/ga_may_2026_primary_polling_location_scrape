"""
Diagnostic: intercept all network requests made by the GA MVP advanced voting
location page so we can identify the Salesforce API endpoint and payload.
Run this once, examine the output, then build the real scraper.
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

        captured = []

        async def handle_response(response):
            url = response.url
            if any(x in url for x in ["aura", "apex", "data", "api", "query", "poll"]):
                try:
                    ct = response.headers.get("content-type", "")
                    if "json" in ct or "javascript" in ct:
                        body = await response.text()
                        captured.append({"url": url, "status": response.status, "body_snippet": body[:800]})
                        print(f"\n[{response.status}] {url}")
                        print(body[:400])
                except Exception:
                    pass

        page.on("response", handle_response)

        print(f"Navigating to: {URL}")
        await page.goto(URL, wait_until="networkidle", timeout=60000)

        # Wait extra for lazy-loaded content
        await page.wait_for_timeout(5000)

        print("\n\n=== PAGE TITLE ===")
        print(await page.title())

        print("\n=== ALL CAPTURED API CALLS ===")
        for item in captured:
            print(f"\n--- {item['url']} ---")
            print(item["body_snippet"][:600])

        # Also dump the full DOM text to see what's rendered
        print("\n\n=== VISIBLE TEXT (first 3000 chars) ===")
        text = await page.inner_text("body")
        print(text[:3000])

        with open("network_log.json", "w") as f:
            json.dump(captured, f, indent=2)
        print("\nFull log saved to network_log.json")

        await browser.close()


asyncio.run(main())
