import logging
import random
import time

import requests
from requests.exceptions import ConnectionError, HTTPError, Timeout

logger = logging.getLogger(__name__)

# Headers proven live on alonhadat/homedy 2026-07-10 via test_scrape_targets.py:
# a realistic desktop UA + vi-VN Accept-Language per CLAUDE.md §6, nothing
# beyond that -- no stealth, no challenge-solving. The old batdongsan Referer
# is gone with the batdongsan crawler itself (Cloudflare-dead per §6).
session = requests.Session()
session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    }
)


def fetch(url):
    for attempt in range(3):
        try:
            response = session.get(url, timeout=10)
        except (Timeout, ConnectionError, HTTPError):
            time.sleep(2**attempt * 2)
            continue

        # alonhadat serves its bot challenge as a 200 redirect to
        # /xac-thuc-nguoi-dung.html (seen live 2026-07-10 after ~30
        # sequential LDP fetches; IP-scoped, SRPs unaffected). Treat it as
        # a failed fetch, no retry: it won't clear in seconds, and solving
        # or routing around it is the evasion CLAUDE.md §6 rules out.
        if "xac-thuc-nguoi-dung" in response.url:
            logger.error(f"bot challenge served for {url}")
            return None

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
