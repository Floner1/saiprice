"""
Diagnostic only. Not part of the scraper.

Answers one question: does a slower navigation cadence clear batdongsan's
Cloudflare challenge, or does the challenge hold regardless of timing?

Opens the same headed Chromium your scraper uses, navigates five pages
of the apartment-sale root with a real delay between each, and reports
whether the listing-link selector appeared (challenge cleared) or timed
out (challenge held) on every navigation.

Run it directly:
    venv/Scripts/python.exe cloudflare_cadence_test.py

Watch the browser window while it runs. Read the printed results after.
"""

import time
from playwright.sync_api import sync_playwright

URLS = [
    "https://batdongsan.com.vn/ban-can-ho-chung-cu-tp-hcm",
    "https://batdongsan.com.vn/ban-can-ho-chung-cu-tp-hcm/p2",
    "https://batdongsan.com.vn/ban-can-ho-chung-cu-tp-hcm/p3",
    "https://batdongsan.com.vn/ban-can-ho-chung-cu-tp-hcm/p4",
    "https://batdongsan.com.vn/ban-can-ho-chung-cu-tp-hcm/p5",
]
SELECTOR = "a.js__product-link-for-product-id"
WAIT_SECONDS = 20  # change this to test other gaps: try 15, 30, 45

results = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    for i, url in enumerate(URLS):
        if i > 0:
            print(f"waiting {WAIT_SECONDS}s before next navigation...")
            time.sleep(WAIT_SECONDS)
        print(f"navigating to {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        try:
            page.wait_for_selector(SELECTOR, timeout=10000)
            results.append((url, "CLEARED"))
        except Exception:
            results.append((url, "CHALLENGE HELD"))
    browser.close()

print("\nRESULTS")
for url, outcome in results:
    print(f"{outcome}: {url}")
