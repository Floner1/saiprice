import logging
import random
import time

import requests
from requests.exceptions import ConnectionError, HTTPError, Timeout

logger = logging.getLogger(__name__)

# ponytail: SRP fetch confirmed 2026-07-07 to need a browser (Cloudflare managed
# JS challenge, 403 on plain requests.get). Per CLAUDE.md §6/§9 that piece is a
# vanilla, local-only browser only -- no stealth, never on Render, not built
# here since scrape_batdongsan.py (the SRP crawl loop) isn't part of this
# session's scope. LDP fetches (this module's job) work fine on plain requests.

session = requests.Session()
session.headers.update(
    {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "vi-VN,vi;q=0.9",
        "Referer": "https://batdongsan.com.vn/",
    }
)


def fetch(url):
    for attempt in range(3):
        try:
            response = session.get(url, timeout=10)
        except (Timeout, ConnectionError, HTTPError):
            time.sleep(2**attempt * 2)
            continue

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else 2**attempt * 2)
            continue
        if response.status_code >= 500:
            time.sleep(2**attempt * 2)
            continue

        time.sleep(random.uniform(1, 3))
        return response

    logger.error(f"gave up on {url} after 3 attempts")
    return None
