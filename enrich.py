#!/usr/bin/env python3
"""
Phone/Email Enricher v2 — uses Playwright to avoid 403s
Rotates user agents, randomizes delays, resets browser every 20 pages.

Run: python3 enrich.py --input output/leads_2026-09-14.csv
"""

import csv, re, time, sys, os, argparse, json, random
from datetime import datetime

SLEEP_MIN  = 4    # min seconds between requests
SLEEP_MAX  = 9    # max seconds between requests
RESET_EVERY = 20  # restart browser every N pages to reset fingerprint

PHONE_RE = re.compile(
    r"(\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4})"
)
EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.208 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.208 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.208 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

def clean_phone(raw):
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits[0] == "1":
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return raw.strip()

def extract_contacts(text):
    phones = []
    for raw in PHONE_RE.findall(text):
        p      = clean_phone(raw)
        digits = re.sub(r"\D", "", p)
        if len(digits) in (10, 11) and p not in phones:
            phones.append(p)

    emails = [
        e for e in EMAIL_RE.findall(text)
        if "zillow" not in e.lower() and len(e) < 60
    ]
    return phones, emails

def make_browser(playwright):
    """Launch a fresh browser with random user agent and viewport."""
    ua = random.choice(USER_AGENTS)
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
    )
    ctx = browser.new_context(
        user_agent=ua,
        viewport={"width": random.randint(1200, 1440),
                  "height": random.randint(700, 900)},
        locale="en-US",
        timezone_id="America/New_York",
        ignore_https_errors=True,
    )
    # Remove webdriver flag
    ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
    """)
    page = ctx.new_page()
    return browser, ctx, page

def visit_listing(page, url):
    """Visit a Zillow listing and extract contact info."""
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        # Random human-like delay
        time.sleep(random.uniform(1.5, 3.0))

        # Scroll down a bit (human behavior)
        page.mouse.wheel(0, random.randint(300, 700))
        time.sleep(random.uniform(0.5, 1.5))

        html    = page.content()
        phones  = []
        emails  = []

        # Try __NEXT_DATA__ JSON first
        from bs4 import BeautifulSoup
        soup   = BeautifulSoup(html, "lxml")
        script = soup.find("script", {"id": "__NEXT_DATA__"})

        full_text = soup.get_text(" ")

        if script:
            try:
                data = json.loads(script.string)
                # Search everywhere in JSON for phone patterns
                json_str = json.dumps(data)
                phones, emails = extract_contacts(json_str)
            except Exception:
                pass

        # Fallback to page text
        if not phones:
            phones, emails = extract_contacts(full_text)

        return phones, emails

    except Exception as e:
        err = str(e)
        if "403" in err or "blocked" in err.lower():
            return None, None   # signal: blocked
        return [], []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",  "-i", required=True)
    parser.add_argument("--output", "-o", default="")
    parser.add_argument("--skip",   "-s", type=int, default=0,
                        help="Skip first N leads (resume from checkpoint)")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"File not found: {args.input}"); sys.exit(1)

    with open(args.input, newline="", encoding="utf-8") as f:
        leads = list(csv.DictReader(f))

    for l in leads:
        if "email" not in l:
            l["email"] = ""

    to_enrich = [(i, l) for i, l in enumerate(leads)
                 if not l.get("phone") and l.get("link")]

    print(f"\n=== Phone/Email Enricher v2 (Playwright) ===")
    print(f"  Total leads:    {len(leads)}")
    print(f"  To enrich:      {len(to_enrich)}")
    print(f"  Est. time:      ~{len(to_enrich) * 6 // 60} min\n")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[!] Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    enriched = 0
    out_path = args.output or args.input

    with sync_playwright() as p:
        browser, ctx, page = make_browser(p)

        # Warm up — visit Zillow homepage first
        print("  Warming up browser...")
        try:
            page.goto("https://www.zillow.com/", timeout=20000,
                      wait_until="domcontentloaded")
            time.sleep(random.uniform(2, 4))
            print("  ✓ Browser ready\n")
        except Exception as e:
            print(f"  [!] Warmup error: {e}")

        for count, (idx, lead) in enumerate(to_enrich):

            # Skip if resuming
            if count < args.skip:
                continue

            # Reset browser every RESET_EVERY pages
            if count > 0 and count % RESET_EVERY == 0:
                print(f"\n  ↻ Resetting browser at lead {count} to avoid detection...\n")
                try:
                    browser.close()
                except Exception:
                    pass
                time.sleep(random.uniform(5, 10))
                browser, ctx, page = make_browser(p)
                # Re-warm
                try:
                    page.goto("https://www.zillow.com/", timeout=20000,
                              wait_until="domcontentloaded")
                    time.sleep(random.uniform(3, 6))
                except Exception:
                    pass

            url   = lead.get("link", "")
            title = lead.get("title", "")[:50]
            print(f"  [{count+1}/{len(to_enrich)}] {title}")

            phones, emails = visit_listing(page, url)

            if phones is None:
                # Hard blocked — save progress and stop
                print(f"\n  [!] Hard blocked at lead {count+1}.")
                print(f"  [!] Resume later with: --skip {count}")
                print(f"  [✓] Saving progress...\n")
                break

            if phones:
                leads[idx]["phone"] = phones[0]
                extra = phones[1:3]
                if extra:
                    leads[idx]["notes"] = leads[idx].get("notes","") + f" | also: {', '.join(extra)}"
                enriched += 1
                print(f"         📞 {phones[0]}", end="")

            if emails:
                leads[idx]["email"] = emails[0]
                print(f"  ✉️  {emails[0]}", end="")

            if not phones and not emails:
                print(f"         — no contact found", end="")

            print()

            # Save checkpoint every 10 leads
            if count % 10 == 0:
                fields = list(leads[0].keys())
                with open(out_path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                    w.writeheader(); w.writerows(leads)

            # Random delay
            delay = random.uniform(SLEEP_MIN, SLEEP_MAX)
            time.sleep(delay)

        try:
            browser.close()
        except Exception:
            pass

    # Final save
    fields = list(leads[0].keys())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(leads)

    print(f"\n  [✓] Enriched {enriched}/{len(to_enrich)} with phone")
    print(f"  [✓] Saved → {out_path}")
    print("\n=== Done ===\n")

if __name__ == "__main__":
    main()
