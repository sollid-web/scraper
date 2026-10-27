#!/usr/bin/env python3
"""
Skip Tracing Module — South Florida Lead Scraper
Provider: BatchData (batchdata.io) — best for real estate investors
Cost:      ~$0.17 per record | 100 records per API call max
Docs:      https://developer.batchdata.io

Usage:
    from skip_trace import enrich_leads
    enriched = enrich_leads(leads, api_key="YOUR_BATCHDATA_KEY")

Or run standalone:
    python3 skip_trace.py --input output/leads_2026-09-14.csv \
                          --key YOUR_BATCHDATA_KEY
"""

import csv, json, os, sys, time, argparse
from datetime import datetime

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "requests", "--break-system-packages", "-q"])
    import requests

# ─── BatchData config ─────────────────────────────────────────
BATCHDATA_URL    = "https://api.batchdata.io/api/v1/property/skip-trace"
BATCH_SIZE       = 100      # max per API call
SLEEP_BETWEEN    = 2        # seconds between batches
CONFIDENCE_MIN   = 60       # skip results below this score (0-100)


# ─── core: single record ──────────────────────────────────────
def skip_trace_single(name: str, address: str, city: str,
                      state: str, zipcode: str, api_key: str) -> dict:
    """
    Look up one owner by name + address.
    Returns dict with phones, emails, confidence.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    payload = {
        "requests": [{
            "firstName": name.split()[0] if name else "",
            "lastName":  " ".join(name.split()[1:]) if len(name.split()) > 1 else "",
            "propertyAddress": {
                "address": address,
                "city":    city,
                "state":   state,
                "zip":     zipcode,
            }
        }]
    }

    try:
        r = requests.post(BATCHDATA_URL, headers=headers,
                          json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        return _parse_response(data, 0)
    except requests.HTTPError as e:
        if e.response.status_code == 401:
            print("  [!] BatchData: Invalid API key.")
        elif e.response.status_code == 402:
            print("  [!] BatchData: Insufficient credits.")
        else:
            print(f"  [!] BatchData HTTP error: {e}")
        return {}
    except Exception as e:
        print(f"  [!] Skip trace error: {e}")
        return {}


# ─── core: bulk records ───────────────────────────────────────
def skip_trace_bulk(records: list[dict], api_key: str) -> list[dict]:
    """
    Bulk skip trace up to 100 records per call.
    Each record needs: owner_name, address, city, state, zip

    Returns the records list enriched with phone/email fields.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    results = []

    # Process in chunks of BATCH_SIZE
    for i in range(0, len(records), BATCH_SIZE):
        chunk = records[i:i + BATCH_SIZE]
        print(f"  → Skip tracing batch {i//BATCH_SIZE + 1} "
              f"({len(chunk)} records)…")

        requests_payload = []
        for rec in chunk:
            name  = rec.get("owner_name", "")
            parts = name.strip().split()
            first = parts[0] if parts else ""
            last  = " ".join(parts[1:]) if len(parts) > 1 else ""

            # Parse address from location field if no dedicated address col
            address = rec.get("address", rec.get("location", ""))

            requests_payload.append({
                "firstName": first,
                "lastName":  last,
                "propertyAddress": {
                    "address": address,
                    "city":    rec.get("city", "Miami"),
                    "state":   "FL",
                    "zip":     rec.get("zip", ""),
                }
            })

        payload = {"requests": requests_payload}

        try:
            r = requests.post(BATCHDATA_URL, headers=headers,
                              json=payload, timeout=60)
            r.raise_for_status()
            data = r.json()

            # Merge results back into original records
            for idx, rec in enumerate(chunk):
                enriched = _parse_response(data, idx)
                rec.update(enriched)
                results.append(rec)

            matched = sum(1 for r in chunk if r.get("phone_1"))
            print(f"    ✓ Matched {matched}/{len(chunk)} with phone")

        except requests.HTTPError as e:
            status = e.response.status_code
            if status == 401:
                print("  [!] Invalid API key — stopping.")
                return results + chunk   # return unenriched remainder
            elif status == 402:
                print("  [!] Out of credits — stopping.")
                return results + chunk
            else:
                print(f"  [!] HTTP {status} — skipping batch.")
                results.extend(chunk)
            continue
        except Exception as e:
            print(f"  [!] Batch error: {e} — skipping.")
            results.extend(chunk)
            continue

        time.sleep(SLEEP_BETWEEN)

    return results


# ─── parse BatchData response ─────────────────────────────────
def _parse_response(data: dict, index: int) -> dict:
    """Extract best phone, email, confidence from BatchData response."""
    try:
        results_list = (
            data.get("results")
            or data.get("data", {}).get("results")
            or []
        )
        if not results_list or index >= len(results_list):
            return {}

        result     = results_list[index]
        confidence = result.get("matchConfidence", 0)

        if confidence < CONFIDENCE_MIN:
            return {"confidence": confidence}

        # Phones — sorted by confidence
        phones = result.get("phones", [])
        phones_sorted = sorted(phones,
                               key=lambda p: p.get("confidence", 0),
                               reverse=True)

        # Emails
        emails = result.get("emails", [])

        # Best mailing address (for pre-foreclosure direct mail)
        mail_addr = result.get("mailingAddress", {})

        return {
            "confidence":    confidence,
            "phone_1":       phones_sorted[0].get("number", "") if phones_sorted else "",
            "phone_1_type":  phones_sorted[0].get("type", "")   if phones_sorted else "",
            "phone_2":       phones_sorted[1].get("number", "") if len(phones_sorted) > 1 else "",
            "email_1":       emails[0].get("email", "") if emails else "",
            "mail_address":  mail_addr.get("address", ""),
            "mail_city":     mail_addr.get("city", ""),
            "mail_state":    mail_addr.get("state", ""),
            "mail_zip":      mail_addr.get("zip", ""),
            "skip_traced":   "yes",
            "traced_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    except Exception:
        return {}


# ─── public function: enrich a lead list ──────────────────────
def enrich_leads(leads: list[dict], api_key: str,
                 only_pre_foreclosure: bool = False) -> list[dict]:
    """
    Main entry point. Pass your leads list + BatchData API key.
    Set only_pre_foreclosure=True to only trace pre-forc leads
    (since FSBO listings often already have phone numbers).
    """
    if not api_key:
        print("  [!] No API key provided — skipping skip trace.")
        return leads

    to_trace = [
        l for l in leads
        if not l.get("phone")   # no phone yet
        and (not only_pre_foreclosure or l.get("type") == "Pre-Foreclosure")
    ]

    already_have = len(leads) - len(to_trace)
    print(f"\n  Skip tracing {len(to_trace)} leads "
          f"({already_have} already have phones)…")

    if not to_trace:
        return leads

    enriched_map = {l["id"]: l for l in skip_trace_bulk(to_trace, api_key)}

    # Merge back
    for lead in leads:
        if lead["id"] in enriched_map:
            lead.update(enriched_map[lead["id"]])

    return leads


# ─── cost estimate ────────────────────────────────────────────
def estimate_cost(num_records: int, price_per: float = 0.17) -> str:
    return f"~${num_records * price_per:.2f} at ${price_per}/record"


# ─── standalone CLI ───────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Skip trace a CSV of leads via BatchData")
    parser.add_argument("--input",  "-i", required=True,
                        help="Input CSV (from scraper.py output)")
    parser.add_argument("--key",    "-k",
                        default=os.environ.get("BATCHDATA_KEY", ""),
                        help="BatchData API key (or set BATCHDATA_KEY env var)")
    parser.add_argument("--output", "-o", default="",
                        help="Output CSV path (default: overwrites input)")
    parser.add_argument("--pre-foreclosure-only", action="store_true",
                        help="Only skip trace Pre-Foreclosure leads")
    args = parser.parse_args()

    if not args.key:
        print("ERROR: No API key. Pass --key or set BATCHDATA_KEY env var.")
        sys.exit(1)

    if not os.path.exists(args.input):
        print(f"ERROR: Input file not found: {args.input}")
        sys.exit(1)

    # Load CSV
    with open(args.input, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        leads  = list(reader)

    print(f"\n=== Skip Trace ===")
    print(f"  Input:    {args.input} ({len(leads)} leads)")
    print(f"  Est cost: {estimate_cost(len(leads))}")

    # Run
    enriched = enrich_leads(
        leads, args.key,
        only_pre_foreclosure=args.pre_foreclosure_only
    )

    # Save
    out_path = args.output or args.input
    if enriched:
        fields = list(enriched[0].keys())
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(enriched)
        phoned = sum(1 for l in enriched if l.get("phone_1"))
        print(f"\n  [✓] {phoned}/{len(enriched)} leads enriched with phone")
        print(f"  [✓] Saved → {out_path}")

    print("\n=== Done ===\n")


if __name__ == "__main__":
    main()

