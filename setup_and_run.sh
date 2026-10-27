#!/bin/bash
# ═══════════════════════════════════════════════════════
#  South Florida Lead Scraper — Full Setup & Run
#  Sources: Zillow FSBO + Broward County Lis Pendens
#  Run: bash setup_and_run.sh
# ═══════════════════════════════════════════════════════

set -e
SCRAPER_DIR="$HOME/scraper"
mkdir -p "$SCRAPER_DIR/output" "$SCRAPER_DIR/debug"
cd "$SCRAPER_DIR"

echo ""
echo "════════════════════════════════════"
echo "  Step 1: Installing dependencies"
echo "════════════════════════════════════"
pip install requests beautifulsoup4 lxml feedparser playwright --prefer-binary -q
playwright install chromium
echo "✓ Dependencies installed"

echo ""
echo "════════════════════════════════════"
echo "  Step 2: Writing scraper"
echo "════════════════════════════════════"

cat > "$SCRAPER_DIR/scraper.py" << 'PYEOF'
#!/usr/bin/env python3
"""
South Florida Lead Scraper v2
Sources:
  1. Zillow FSBO        — active for-sale-by-owner listings
  2. Broward County     — Lis Pendens (pre-foreclosure) public records
Output: output/leads_YYYY-MM-DD.csv
"""

import csv, hashlib, os, re, time, json
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import requests

OUTPUT_DIR = "./output"
SEEN_FILE  = "./output/seen_ids.txt"
DEBUG_DIR  = "./debug"
SLEEP      = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.6367.208 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}

def uid(s):
    return hashlib.md5(s.encode()).hexdigest()[:12]

def load_seen():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if not os.path.exists(SEEN_FILE):
        return set()
    return set(open(SEEN_FILE).read().splitlines())

def save_seen(ids):
    open(SEEN_FILE, "w").write("\n".join(ids))

def save_csv(leads, path):
    if not leads:
        print("  [i] No new leads."); return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=leads[0].keys())
        w.writeheader(); w.writerows(leads)
    print(f"  [✓] {len(leads)} leads → {path}")

def lead(id, type, source, city, title, price, location,
         date_posted, link, owner_name="", phone="", notes=""):
    return {
        "id": id, "type": type, "source": source,
        "city": city, "title": title, "price": price,
        "location": location, "date_posted": date_posted,
        "link": link, "owner_name": owner_name,
        "phone": phone, "notes": notes,
    }

# ── SOURCE 1: ZILLOW FSBO ─────────────────────────────────────
def scrape_zillow_fsbo():
    leads = []
    cities = [
        ("Miami",         "25.7617,-80.1918",  30),
        ("Fort Lauderdale","26.1224,-80.1373", 25),
        ("West Palm Beach","26.7153,-80.0534", 25),
        ("Boca Raton",    "26.3683,-80.1289",  20),
        ("Hialeah",       "25.8576,-80.2781",  15),
    ]

    for city_name, coords, radius in cities:
        print(f"  → Zillow FSBO [{city_name}]")
        lat, lon = coords.split(",")

        # Zillow search API (JSON endpoint)
        url = "https://www.zillow.com/search/GetSearchPageState.htm"
        params = {
            "searchQueryState": json.dumps({
                "pagination": {},
                "mapBounds": {},
                "isMapVisible": True,
                "filterState": {
                    "isForSaleByOwner": {"value": True},
                    "isForSaleByAgent": {"value": False},
                    "isNewConstruction": {"value": False},
                    "isAuction": {"value": False},
                    "isComingSoon": {"value": False},
                },
                "isListVisible": True,
                "regionSelection": [{"regionId": 0}],
                "mapZoom": 11,
                "customRegionId": None,
            }),
            "wants": json.dumps({"cat1": ["listResults"], "cat2": ["total"]}),
            "requestId": 2,
        }

        session = requests.Session()
        session.headers.update(HEADERS)

        # Must visit Zillow homepage first to get cookies
        try:
            session.get("https://www.zillow.com", timeout=15)
            time.sleep(2)
        except Exception:
            pass

        # Search by city name instead
        search_url = f"https://www.zillow.com/homes/for_sale/{city_name.replace(' ','-')}-FL_rb/"
        try:
            r = session.get(search_url, timeout=20, params={"searchQueryState": json.dumps({
                "filterState": {
                    "isForSaleByOwner": {"value": True},
                    "isForSaleByAgent": {"value": False},
                }
            })})

            # Save debug
            os.makedirs(DEBUG_DIR, exist_ok=True)
            with open(f"{DEBUG_DIR}/zillow_{city_name.replace(' ','_')}.html", "w", encoding="utf-8", errors="replace") as f:
                f.write(r.text)

            soup = BeautifulSoup(r.text, "lxml")

            # Extract from Next.js data blob
            script = soup.find("script", {"id": "__NEXT_DATA__"})
            if script:
                data = json.loads(script.string)
                results = (
                    data.get("props", {})
                        .get("pageProps", {})
                        .get("searchPageState", {})
                        .get("cat1", {})
                        .get("searchResults", {})
                        .get("listResults", [])
                )
                for item in results:
                    zpid     = str(item.get("zpid", ""))
                    title    = item.get("addressStreet", "")
                    price    = item.get("price", "N/A")
                    city_val = item.get("addressCity", city_name)
                    state    = item.get("addressState", "FL")
                    zipcode  = item.get("addressZipcode", "")
                    link     = "https://www.zillow.com" + item.get("detailUrl", "")
                    beds     = item.get("beds", "")
                    baths    = item.get("baths", "")
                    sqft     = item.get("area", "")
                    status   = item.get("statusText", "")
                    lat_val  = item.get("latLong", {}).get("latitude", "")
                    lon_val  = item.get("latLong", {}).get("longitude", "")

                    if not zpid:
                        continue

                    leads.append(lead(
                        id=uid(zpid),
                        type="FSBO",
                        source="Zillow",
                        city=city_name,
                        title=f"{title}, {city_val}, {state} {zipcode}",
                        price=f"${price:,}" if isinstance(price, int) else str(price),
                        location=f"{city_val}, {state}",
                        date_posted=datetime.now().strftime("%Y-%m-%d"),
                        link=link,
                        notes=f"{beds}bd/{baths}ba {sqft}sqft | {status}",
                    ))
                print(f"    Found {len(results)} listings")
            else:
                print(f"    [!] No __NEXT_DATA__ found — saved debug HTML")

        except Exception as e:
            print(f"    [!] Error: {e}")

        time.sleep(SLEEP)

    return leads


# ── SOURCE 2: BROWARD COUNTY LIS PENDENS (Playwright) ─────────
def scrape_broward_lis_pendens():
    leads = []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [!] Playwright not installed"); return leads

    print("  → Broward County Clerk: Lis Pendens")
    today    = datetime.now().strftime("%m/%d/%Y")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%m/%d/%Y")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx     = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        try:
            print("    Loading Broward Clerk search...")
            page.goto(
                "https://officialrecords.broward.org/AcclaimWeb/search/SearchTypeDocType",
                timeout=40000,
                wait_until="domcontentloaded",
            )

            # Screenshot what we see
            page.screenshot(path=f"{DEBUG_DIR}/broward_loaded.png")
            print(f"    Title: {page.title()}")
            print(f"    URL: {page.url}")

            # Try filling the form
            page.wait_for_timeout(2000)

            # Doc type field
            try:
                page.fill("input[name='DocTypeID']", "LP")
            except:
                try:
                    page.select_option("select[name='DocTypeID']", label="LIS PENDENS")
                except:
                    print("    [!] Could not find doc type field")

            # Date range
            try:
                page.fill("input[name='DateFrom']", week_ago)
                page.fill("input[name='DateTo']",   today)
            except Exception as e:
                print(f"    [!] Date fields: {e}")

            # Submit
            try:
                page.click("input[type='submit'], button[type='submit']")
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(3000)
            except Exception as e:
                print(f"    [!] Submit: {e}")

            # Screenshot results
            page.screenshot(path=f"{DEBUG_DIR}/broward_results.png", full_page=True)
            html = page.content()
            with open(f"{DEBUG_DIR}/broward_results.html", "w", encoding="utf-8") as f:
                f.write(html)
            print(f"    Saved debug → debug/broward_results.png")

            # Parse results
            soup  = BeautifulSoup(html, "lxml")
            rows  = soup.select("table tr")[1:]
            print(f"    Rows found: {len(rows)}")

            for row in rows:
                cols = [td.get_text(strip=True) for td in row.select("td")]
                if len(cols) < 4:
                    continue
                try:
                    grantor     = cols[2] if len(cols) > 2 else ""
                    record_date = cols[0] if len(cols) > 0 else ""
                    doc_type    = " ".join(cols).upper()
                    if "LIS PENDENS" not in doc_type and "LP" not in doc_type:
                        continue
                    book    = cols[3] if len(cols) > 3 else ""
                    pg      = cols[4] if len(cols) > 4 else ""
                    doc_link = f"https://officialrecords.broward.org/AcclaimWeb/search/Disclaimer?Y=1"

                    leads.append(lead(
                        id=uid(f"broward{grantor}{record_date}"),
                        type="Pre-Foreclosure",
                        source="Broward County Clerk",
                        city="Broward County",
                        title=f"Lis Pendens — {grantor}",
                        price="N/A",
                        location=f"Broward County, FL",
                        date_posted=record_date,
                        link=doc_link,
                        owner_name=grantor,
                        notes=f"Book {book} Pg {pg}",
                    ))
                except Exception as e:
                    continue

        except Exception as e:
            print(f"    [!] Playwright error: {e}")
            page.screenshot(path=f"{DEBUG_DIR}/broward_error.png")
        finally:
            browser.close()

    return leads


# ── MAIN ──────────────────────────────────────────────────────
def main():
    print(f"\n=== South Florida Lead Scraper v2 ===")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    seen      = load_seen()
    all_leads = []

    # 1. Zillow FSBO
    zillow_leads = scrape_zillow_fsbo()
    print(f"\n  Zillow raw: {len(zillow_leads)}")

    # 2. Broward Lis Pendens
    broward_leads = scrape_broward_lis_pendens()
    print(f"  Broward raw: {len(broward_leads)}\n")

    # Dedup
    for l in zillow_leads + broward_leads:
        if l["id"] not in seen:
            seen.add(l["id"])
            all_leads.append(l)

    all_leads.sort(key=lambda x: x["date_posted"], reverse=True)
    print(f"  New leads: {len(all_leads)}")

    date_str = datetime.now().strftime("%Y-%m-%d")
    out_path = os.path.join(OUTPUT_DIR, f"leads_{date_str}.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    save_csv(all_leads, out_path)
    save_seen(seen)

    print("\n=== Done ===\n")
    print(f"  Check output/ for CSV")
    print(f"  Check debug/ for screenshots if issues\n")

if __name__ == "__main__":
    main()
PYEOF

echo "✓ scraper.py written"

echo ""
echo "════════════════════════════════════"
echo "  Step 3: Running scraper"
echo "════════════════════════════════════"
python3 "$SCRAPER_DIR/scraper.py"

echo ""
echo "════════════════════════════════════"
echo "  Step 4: Results"
echo "════════════════════════════════════"
echo ""
echo "CSV files:"
ls -lh "$SCRAPER_DIR/output/" 2>/dev/null || echo "  (none yet)"
echo ""
echo "Debug files:"
ls -lh "$SCRAPER_DIR/debug/" 2>/dev/null || echo "  (none yet)"


