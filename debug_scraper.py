#!/usr/bin/env python3
"""
Debug script — saves HTML + PNG screenshots for both sources
Run: python3 debug_scraper.py
Check: debug/ folder for output files
"""

import os, time
import feedparser
import requests
from bs4 import BeautifulSoup

DEBUG_DIR = "./debug"
os.makedirs(DEBUG_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# ── 1. Craigslist RSS debug ────────────────────────────────────
print("\n=== DEBUG: Craigslist RSS ===")

CL_FEEDS = {
    "Miami":           "https://miami.craigslist.org/search/reo?format=rss&purveyor=owner",
    "Fort_Lauderdale": "https://miami.craigslist.org/search/brw/reo?format=rss&purveyor=owner",
}

for city, url in CL_FEEDS.items():
    print(f"\n→ Testing [{city}]: {url}")
    try:
        # Try raw requests first to see what we actually get
        r = requests.get(url, headers=HEADERS, timeout=15)
        print(f"  HTTP status: {r.status_code}")
        print(f"  Content-Type: {r.headers.get('Content-Type', 'unknown')}")
        print(f"  Response size: {len(r.content)} bytes")

        # Save raw response
        html_path = f"{DEBUG_DIR}/cl_{city}_raw.html"
        with open(html_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(r.text)
        print(f"  Saved raw → {html_path}")

        # Show first 500 chars
        print(f"  Preview: {r.text[:500]}")

        # Try feedparser on the raw content
        feed = feedparser.parse(r.content)
        print(f"  feedparser entries: {len(feed.entries)}")
        print(f"  feedparser status: {feed.get('status', 'N/A')}")
        if feed.entries:
            print(f"  First entry: {feed.entries[0].get('title', 'no title')}")

    except Exception as e:
        print(f"  ERROR: {e}")
    time.sleep(2)

# ── 2. Craigslist HTML (non-RSS) fallback debug ────────────────
print("\n\n=== DEBUG: Craigslist HTML page ===")
cl_html_url = "https://miami.craigslist.org/search/reo?purveyor=owner&sort=date"
try:
    r = requests.get(cl_html_url, headers=HEADERS, timeout=15)
    print(f"  HTTP status: {r.status_code}")
    html_path = f"{DEBUG_DIR}/cl_miami_html.html"
    with open(html_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(r.text)
    print(f"  Saved → {html_path}")
    soup = BeautifulSoup(r.text, "lxml")
    items = soup.select("li.cl-search-result") or soup.select("li.result-row")
    print(f"  Items found: {len(items)}")
    print(f"  Page title: {soup.title.string if soup.title else 'none'}")
    print(f"  Preview: {r.text[:300]}")
except Exception as e:
    print(f"  ERROR: {e}")

# ── 3. Miami-Dade Clerk debug (Playwright + screenshot) ────────
print("\n\n=== DEBUG: Miami-Dade Clerk (Playwright) ===")
try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            ignore_https_errors=True,   # bypass SSL issues
        )
        page = context.new_page()

        # Test 1: try www2 subdomain
        print("\n→ Trying: https://www2.miamidadeclerk.com/ocs/Search.aspx")
        try:
            page.goto(
                "https://www2.miamidadeclerk.com/ocs/Search.aspx",
                timeout=30000,
                wait_until="domcontentloaded"   # less strict than networkidle
            )
            png_path = f"{DEBUG_DIR}/mdc_www2.png"
            html_path = f"{DEBUG_DIR}/mdc_www2.html"
            page.screenshot(path=png_path, full_page=True)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(page.content())
            print(f"  Title: {page.title()}")
            print(f"  URL after redirect: {page.url}")
            print(f"  Screenshot → {png_path}")
            print(f"  HTML → {html_path}")
        except Exception as e:
            print(f"  ERROR: {e}")
            page.screenshot(path=f"{DEBUG_DIR}/mdc_www2_error.png")
            print(f"  Error screenshot → {DEBUG_DIR}/mdc_www2_error.png")

        # Test 2: try main domain
        print("\n→ Trying: https://www.miamidadeclerk.com")
        try:
            page.goto(
                "https://www.miamidadeclerk.com",
                timeout=30000,
                wait_until="domcontentloaded"
            )
            png_path = f"{DEBUG_DIR}/mdc_main.png"
            page.screenshot(path=png_path, full_page=True)
            print(f"  Title: {page.title()}")
            print(f"  URL: {page.url}")
            print(f"  Screenshot → {png_path}")
        except Exception as e:
            print(f"  ERROR: {e}")

        browser.close()

except ImportError:
    print("  Playwright not installed — run: pip install playwright && playwright install chromium")
except Exception as e:
    print(f"  Playwright ERROR: {e}")

print("\n\n=== Debug complete ===")
print(f"Check the '{DEBUG_DIR}/' folder for HTML files and PNG screenshots")
print("Share any screenshots here so we can see what the sites are returning\n")

