#!/usr/bin/env python3
"""
South Florida Lead Scraper v4
Sources:  Craigslist (by owner) + ForSaleByOwner.com + ByOwner.com
Filter:   Auto-tags leads by type agents love most
Output:   output/leads_YYYY-MM-DD.csv
Run:      python3 scraper.py
"""

import csv, hashlib, os, re, time
from datetime import datetime
from bs4 import BeautifulSoup
from worker.poster_filter import classify_poster

OUTPUT_DIR = "./output"
SEEN_FILE  = "./output/seen_ids.txt"
DEBUG_DIR  = "./debug"

POLITE_DELAY_SECONDS = 3

PHONE_RE = re.compile(r"(\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4})")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PRICE_RE = re.compile(r"\$[\d,]+")

# ── LEAD TYPE FILTERS — what agents love most ─────────────────
LEAD_TYPES = {
    "🏚️ HEIR/ESTATE": [
        "estate sale", "estate of", "heir", "probate", "inherited",
        "inheritance", "trust sale", "deceased", "executor",
        "selling estate", "family estate",
    ],
    "⏰ EXPIRED/RELISTED": [
        "was listed", "back on market", "price reduced",
        "reduced from", "previously listed", "relisted",
        "dropped price", "price improvement", "new price",
    ],
    "🚨 MOTIVATED SELLER": [
        "must sell", "motivated", "need to sell", "needs to sell",
        "relocating", "job transfer", "divorce", "divorcing",
        "moving out of state", "moving out of country",
        "financial hardship", "behind on payments", "can't afford",
        "make offer", "all offers considered", "serious offers",
        "below market", "below appraisal", "under market value",
    ],
    "🔨 AS-IS/INVESTOR": [
        "as-is", "as is", "fixer", "fixer upper", "investor special",
        "cash only", "cash preferred", "handyman", "tlc needed",
        "needs work", "needs repairs", "project home",
        "flip opportunity", "fix and flip", "investment property",
    ],
    "🏠 VACANT/MOVE-IN": [
        "vacant", "empty", "move in ready", "move-in ready",
        "immediate occupancy", "available now", "ready to move",
        "no tenants", "owner occupied",
    ],
    "💰 PRICE REDUCED": [
        "price drop", "price cut", "just reduced", "newly reduced",
        "drastically reduced", "below tax value", "steal",
        "motivated price",
    ],
    "🌴 NEW LISTING": [],   # Filled by date logic — posted < 48hrs ago
}

def classify_lead(title, description, date_posted):
    """Return the best matching lead type for agents."""
    text = (title + " " + description).lower()
    matches = []

    for lead_type, keywords in LEAD_TYPES.items():
        if lead_type == "🌴 NEW LISTING":
            continue
        for kw in keywords:
            if kw in text:
                matches.append(lead_type)
                break

    # New listing check — posted within 48 hours
    try:
        posted = datetime.strptime(date_posted[:10], "%Y-%m-%d")
        if (datetime.now() - posted).days <= 2:
            matches.append("🌴 NEW LISTING")
    except Exception:
        pass

    if not matches:
        return "📋 STANDARD FSBO"
    return " + ".join(matches)

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

def clean_phone(raw):
    d = re.sub(r"\D", "", raw)
    if len(d) == 10:   return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    if len(d) == 11 and d[0] == "1": return f"({d[1:4]}) {d[4:7]}-{d[7:]}"
    return raw.strip()

def extract_phone(text):
    for raw in PHONE_RE.findall(text):
        p = clean_phone(raw)
        d = re.sub(r"\D", "", p)
        if len(d) in (10, 11):
            return p
    return ""

def extract_email(text):
    for e in EMAIL_RE.findall(text):
        if len(e) < 60:
            return e
    return ""

def make_lead(**kw):
    return {
        "id":          kw.get("id", ""),
        "lead_type":   kw.get("lead_type", "📋 STANDARD FSBO"),
        "type":        kw.get("type", "FSBO"),
        "source":      kw.get("source", ""),
        "city":        kw.get("city", ""),
        "title":       kw.get("title", ""),
        "price":       kw.get("price", "N/A"),
        "beds_baths":  kw.get("beds_baths", ""),
        "location":    kw.get("location", ""),
        "date_posted": kw.get("date_posted", ""),
        "link":        kw.get("link", ""),
        "owner_name":  kw.get("owner_name", ""),
        "phone":       kw.get("phone", ""),
        "email":       kw.get("email", ""),
        "description": kw.get("description", ""),
        "poster_decision": kw.get("poster_decision", ""),
        "poster_decision_reason": kw.get("poster_decision_reason", ""),
        "poster_evidence": kw.get("poster_evidence", ""),
    }

def make_browser(playwright):
    """Use a standard browser context; do not disguise automation or bypass TLS."""
    b = playwright.chromium.launch(headless=True)
    ctx = b.new_context()
    return b, ctx.new_page()

def sleep():
    """Apply a fixed courtesy delay between requests."""
    time.sleep(POLITE_DELAY_SECONDS)

# ── SOURCE 1: CRAIGSLIST BY OWNER ─────────────────────────────
CRAIGSLIST_SEARCHES = [
    ("Miami",           "https://miami.craigslist.org/search/reo?purveyor=owner&sort=date"),
    ("Fort Lauderdale", "https://miami.craigslist.org/search/brw/reo?purveyor=owner&sort=date"),
    ("West Palm Beach", "https://miami.craigslist.org/search/pab/reo?purveyor=owner&sort=date"),
    ("Broward",         "https://miami.craigslist.org/search/brd/reo?purveyor=owner&sort=date"),
]

BROKER_WORDS = re.compile(
    r"\b(realtor|broker|realty|brokerage|mls|dre\s*#|lic\.|"
    r"london foster|compass|keller|coldwell|century 21|re/max|"
    r"sotheby|engel|exp realty)\b", re.IGNORECASE
)

def scrape_craigslist(page, cities=None, limit=None):
    leads = []
    searches = [(city, url) for city, url in CRAIGSLIST_SEARCHES
                if not cities or city in cities]
    for city, url in searches:
        if limit is not None and len(leads) >= limit:
            break
        print(f"  → Craigslist [{city}]")
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            sleep()
            time.sleep(2)

            html = page.content()
            with open(f"{DEBUG_DIR}/cl_{city.replace(' ','_')}.html","w",
                      encoding="utf-8", errors="replace") as f:
                f.write(html)

            soup  = BeautifulSoup(html, "lxml")
            items = soup.select("li.cl-search-result, li.result-row")
            print(f"    Found {len(items)} listings")

            for item in items:
                if limit is not None and len(leads) >= limit:
                    break
                try:
                    # Title + link
                    a = item.select_one("a.posting-title, a.result-title")
                    if not a: continue
                    title = a.get_text(strip=True)
                    link  = a.get("href","")
                    if not link.startswith("http"):
                        link = "https://miami.craigslist.org" + link

                    decision = classify_poster("craigslist", title)
                    if decision["decision"] == "reject_agent":
                        leads.append(make_lead(
                            id=uid(link), source="Craigslist", city=city,
                            title=title, location=city + ", FL", link=link,
                            poster_decision=decision["decision"],
                            poster_decision_reason=decision["reason_code"],
                            poster_evidence=";".join(decision["evidence_codes"]),
                        ))
                        continue

                    # Price
                    price_tag = item.select_one(".priceinfo, .result-price")
                    price     = price_tag.get_text(strip=True) if price_tag else "N/A"

                    # Date
                    dt  = item.select_one("time")
                    date_posted = dt["datetime"][:10] if dt and dt.get("datetime") else datetime.now().strftime("%Y-%m-%d")

                    # Visit listing page for phone + description
                    phone, email, description, owner_name = "", "", "", ""
                    try:
                        page.goto(link, timeout=25000, wait_until="domcontentloaded")
                        time.sleep(2)
                        detail_html = page.content()
                        detail_soup = BeautifulSoup(detail_html, "lxml")
                        detail_text = detail_soup.get_text(" ")

                        body = detail_soup.select_one("#postingbody")
                        description = body.get_text(" ", strip=True)[:400] if body else ""

                        name_tag = detail_soup.select_one(".reply_options b")
                        owner_name = name_tag.get_text(strip=True) if name_tag else ""

                        decision = classify_poster("craigslist", title, detail_text, owner_name)
                        if decision["decision"] != "owner_candidate":
                            leads.append(make_lead(
                                id=uid(link), source="Craigslist", city=city,
                                title=title, location=city + ", FL", link=link,
                                poster_decision=decision["decision"],
                                poster_decision_reason=decision["reason_code"],
                                poster_evidence=";".join(decision["evidence_codes"]),
                            ))
                            page.go_back()
                            continue

                        phone = extract_phone(detail_text)
                        email = extract_email(detail_text)

                        page.go_back()
                        time.sleep(1)
                    except Exception:
                        pass

                    lead_type = classify_lead(title, description, date_posted)

                    leads.append(make_lead(
                        id          = uid(link),
                        lead_type   = lead_type,
                        source      = "Craigslist",
                        city        = city,
                        title       = title,
                        price       = price,
                        location    = city + ", FL",
                        date_posted = date_posted,
                        link        = link,
                        owner_name  = owner_name,
                        phone       = phone,
                        email       = email,
                        description = description,
                        poster_decision=decision["decision"],
                        poster_decision_reason=decision["reason_code"],
                        poster_evidence=";".join(decision["evidence_codes"]),
                    ))
                    sleep()

                except Exception as e:
                    print(f"    [!] Item error: {e}")
                    continue

        except Exception as e:
            print(f"    [!] City error: {e}")
            with open(f"{DEBUG_DIR}/cl_{city}_error.png","wb") as f:
                try: f.write(page.screenshot())
                except: pass
        sleep()
    return leads


# ── SOURCE 2: FORSALEBYOWNER.COM ──────────────────────────────
FSBO_SEARCHES = [
    ("Miami",           "https://www.forsalebyowner.com/real-estate/fl/miami/"),
    ("Fort Lauderdale", "https://www.forsalebyowner.com/real-estate/fl/fort-lauderdale/"),
    ("West Palm Beach", "https://www.forsalebyowner.com/real-estate/fl/west-palm-beach/"),
    ("Boca Raton",      "https://www.forsalebyowner.com/real-estate/fl/boca-raton/"),
    ("Hialeah",         "https://www.forsalebyowner.com/real-estate/fl/hialeah/"),
]

def scrape_forsalebyowner(page, cities=None, limit=None):
    leads = []
    searches = [(city, url) for city, url in FSBO_SEARCHES
                if not cities or city in cities]
    for city, url in searches:
        if limit is not None and len(leads) >= limit:
            break
        print(f"  → ForSaleByOwner.com [{city}]")
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            sleep()

            html = page.content()
            with open(f"{DEBUG_DIR}/fsbo_{city.replace(' ','_')}.html","w",
                      encoding="utf-8", errors="replace") as f:
                f.write(html)

            soup     = BeautifulSoup(html, "lxml")
            listings = soup.select(
                "div.listing-card, div.property-card, "
                "article.listing, div[class*='listing']"
            )
            print(f"    Found {len(listings)} listings")

            for listing in listings:
                if limit is not None and len(leads) >= limit:
                    break
                try:
                    # Title / address
                    addr_tag = listing.select_one(
                        "h2, h3, .address, .listing-address, [class*='address']"
                    )
                    if not addr_tag: continue
                    title = addr_tag.get_text(strip=True)
                    if not title or len(title) < 5: continue

                    decision = classify_poster("forsalebyowner", title)
                    if decision["decision"] == "reject_agent":
                        leads.append(make_lead(
                            id=uid(title), source="ForSaleByOwner.com", city=city,
                            title=title, location=f"{city}, FL",
                            poster_decision=decision["decision"],
                            poster_decision_reason=decision["reason_code"],
                            poster_evidence=";".join(decision["evidence_codes"]),
                        ))
                        continue

                    # Link
                    a    = listing.select_one("a[href]")
                    link = a["href"] if a else ""
                    if link and not link.startswith("http"):
                        link = "https://www.forsalebyowner.com" + link

                    # Price
                    price_tag = listing.select_one(
                        ".price, .listing-price, [class*='price']"
                    )
                    price = price_tag.get_text(strip=True) if price_tag else "N/A"

                    # Beds/Baths
                    bb_tag = listing.select_one(
                        ".beds-baths, .details, [class*='beds'], [class*='bath']"
                    )
                    beds_baths = bb_tag.get_text(strip=True) if bb_tag else ""

                    date_posted = datetime.now().strftime("%Y-%m-%d")
                    phone, email, description = "", "", ""

                    # Visit detail page if we have a link
                    if link:
                        try:
                            page.goto(link, timeout=25000, wait_until="domcontentloaded")
                            time.sleep(2)
                            d_html = page.content()
                            d_soup = BeautifulSoup(d_html, "lxml")
                            d_text = d_soup.get_text(" ")

                            desc_tag = d_soup.select_one(
                                ".description, .listing-description, [class*='description']"
                            )
                            description = desc_tag.get_text(" ", strip=True)[:400] if desc_tag else d_text[:400]

                            decision = classify_poster("forsalebyowner", title, d_text)
                            if decision["decision"] != "owner_candidate":
                                page.go_back()
                                leads.append(make_lead(
                                    id=uid(link or title), source="ForSaleByOwner.com", city=city,
                                    title=title, location=f"{city}, FL", link=link,
                                    poster_decision=decision["decision"],
                                    poster_decision_reason=decision["reason_code"],
                                    poster_evidence=";".join(decision["evidence_codes"]),
                                ))
                                continue

                            phone = extract_phone(d_text)
                            email = extract_email(d_text)

                            page.go_back()
                            time.sleep(2)
                        except Exception:
                            pass

                    lead_type = classify_lead(title, description, date_posted)

                    leads.append(make_lead(
                        id          = uid(link or title),
                        lead_type   = lead_type,
                        source      = "ForSaleByOwner.com",
                        city        = city,
                        title       = title,
                        price       = price,
                        beds_baths  = beds_baths,
                        location    = f"{city}, FL",
                        date_posted = date_posted,
                        link        = link,
                        phone       = phone,
                        email       = email,
                        description = description,
                    ))
                    sleep()

                except Exception as e:
                    print(f"    [!] Listing error: {e}")
                    continue

        except Exception as e:
            print(f"    [!] City error: {e}")
            try:
                page.screenshot(path=f"{DEBUG_DIR}/fsbo_{city}_error.png")
            except: pass
        sleep()
    return leads


# ── SOURCE 3: BYOWNER.COM ─────────────────────────────────────
BYOWNER_SEARCHES = [
    ("Miami",         "https://www.byowner.com/buy/fl/miami-real-estate/"),
    ("Broward",       "https://www.byowner.com/buy/fl/fort-lauderdale-real-estate/"),
    ("Palm Beach",    "https://www.byowner.com/buy/fl/west-palm-beach-real-estate/"),
]

def scrape_byowner(page, cities=None, limit=None):
    leads = []
    searches = [(city, url) for city, url in BYOWNER_SEARCHES
                if not cities or city in cities]
    for city, url in searches:
        if limit is not None and len(leads) >= limit:
            break
        print(f"  → ByOwner.com [{city}]")
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            sleep()

            html = page.content()
            with open(f"{DEBUG_DIR}/byowner_{city.replace(' ','_')}.html","w",
                      encoding="utf-8", errors="replace") as f:
                f.write(html)

            soup     = BeautifulSoup(html, "lxml")
            listings = soup.select(
                "div.listing, article, div[class*='property'], "
                "div[class*='listing'], li[class*='listing']"
            )
            print(f"    Found {len(listings)} listings")

            for listing in listings:
                if limit is not None and len(leads) >= limit:
                    break
                try:
                    a = listing.select_one("a[href]")
                    if not a: continue
                    link  = a.get("href","")
                    title = a.get_text(strip=True) or listing.get_text(" ",strip=True)[:80]
                    if not link.startswith("http"):
                        link = "https://www.byowner.com" + link

                    decision = classify_poster("byowner", title)
                    if decision["decision"] == "reject_agent":
                        leads.append(make_lead(
                            id=uid(link or title), source="ByOwner.com", city=city,
                            title=title, location=f"{city}, FL", link=link,
                            poster_decision=decision["decision"],
                            poster_decision_reason=decision["reason_code"],
                            poster_evidence=";".join(decision["evidence_codes"]),
                        ))
                        continue

                    price_tag  = listing.select_one("[class*='price']")
                    price      = price_tag.get_text(strip=True) if price_tag else "N/A"
                    date_posted = datetime.now().strftime("%Y-%m-%d")
                    phone, email, description = "", "", ""

                    if link:
                        try:
                            page.goto(link, timeout=25000, wait_until="domcontentloaded")
                            time.sleep(2)
                            d_text = BeautifulSoup(page.content(), "lxml").get_text(" ")
                            description = d_text[:400]
                            decision = classify_poster("byowner", title, d_text)
                            if decision["decision"] != "owner_candidate":
                                page.go_back(); time.sleep(2)
                                leads.append(make_lead(
                                    id=uid(link or title), source="ByOwner.com", city=city,
                                    title=title, location=f"{city}, FL", link=link,
                                    poster_decision=decision["decision"],
                                    poster_decision_reason=decision["reason_code"],
                                    poster_evidence=";".join(decision["evidence_codes"]),
                                ))
                                continue
                            phone  = extract_phone(d_text)
                            email  = extract_email(d_text)
                            page.go_back(); time.sleep(2)
                        except Exception:
                            pass

                    lead_type = classify_lead(title, description, date_posted)

                    leads.append(make_lead(
                        id          = uid(link or title),
                        lead_type   = lead_type,
                        source      = "ByOwner.com",
                        city        = city,
                        title       = title,
                        price       = price,
                        location    = f"{city}, FL",
                        date_posted = date_posted,
                        link        = link,
                        phone       = phone,
                        email       = email,
                        description = description,
                    ))
                    sleep()

                except Exception as e:
                    continue

        except Exception as e:
            print(f"    [!] {e}")
        sleep()
    return leads


# ── PRINT SUMMARY BY LEAD TYPE ────────────────────────────────
def print_summary(leads):
    from collections import Counter
    counts = Counter(l["lead_type"] for l in leads)
    has_phone = sum(1 for l in leads if l.get("phone"))
    print(f"\n  ── Lead Type Breakdown ──")
    for lt, count in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {lt:<35} {count} leads")
    print(f"\n  Total:      {len(leads)}")
    print(f"  Has phone:  {has_phone} ({has_phone*100//max(len(leads),1)}%)")


# ── MAIN ──────────────────────────────────────────────────────
SOURCE_ADAPTERS = {
    "craigslist": ("Craigslist", scrape_craigslist, CRAIGSLIST_SEARCHES),
    "forsalebyowner": ("ForSaleByOwner.com", scrape_forsalebyowner, FSBO_SEARCHES),
    "byowner": ("ByOwner.com", scrape_byowner, BYOWNER_SEARCHES),
}
SUPPORTED_CITIES = sorted({city for _, _, searches in SOURCE_ADAPTERS.values()
                           for city, _ in searches})


def main(argv=None):
    """Delegate CLI handling to the isolated, dry-run-first worker."""
    from worker.leadforge_worker import main as worker_main
    return worker_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
