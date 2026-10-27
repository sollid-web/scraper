#!/usr/bin/env python3
"""Opt-in, isolated worker wrapper for the historical listing adapters.

No adapter is called unless --collect and a non-empty authorization reference
are both supplied. The authorization reference is recorded only as a boolean;
its actual value is never emitted in JSONL status or CSV.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Callable

# In the repository and Docker image scraper.py is the sibling of worker/.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import scraper

SOURCE_ADAPTERS = scraper.SOURCE_ADAPTERS
SOURCE_CITIES = {
    key: {city for city, _url in adapter[2]}
    for key, adapter in SOURCE_ADAPTERS.items()
}
ALL_CITIES = sorted(set().union(*SOURCE_CITIES.values()))
LEAD_FIELDS = [
    "id", "lead_type", "type", "source", "city", "title", "price",
    "beds_baths", "location", "date_posted", "link", "owner_name",
    "phone", "email", "description",
]
MANIFEST_FIELDS = LEAD_FIELDS + ["run_id"]
LEADFORGE_FIELDS = MANIFEST_FIELDS + [
    "source_name", "source_url", "external_id", "country_code", "region",
    "currency", "listing_type", "property_type", "poster_type",
    "poster_evidence", "published_at",
]


def stable_run_id(config: dict) -> str:
    """Derive a repeatable run identifier from collection parameters."""
    identity = {
        "country": config["country"],
        "cities": sorted(config["cities"]) if config["cities"] else None,
        "intent": config["intent"],
        "sources": sorted(config["sources"]),
        "limit": config["limit"],
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return "run-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def validate_config(raw: dict) -> tuple[dict, list[str]]:
    """Validate only capabilities represented by the existing adapters."""
    errors = []
    country = str(raw.get("country", "US")).upper()
    intent = str(raw.get("intent", "sale")).lower()
    sources = raw.get("sources") or list(SOURCE_ADAPTERS)
    cities = raw.get("cities")
    limit = raw.get("limit", 100)
    if country != "US":
        errors.append(f"unsupported country {country!r}; only US is configured")
    if intent != "sale":
        errors.append(f"unsupported listing intent {intent!r}; only sale is supported")
    if raw.get("property_type"):
        errors.append("unsupported filter: property type cannot be reliably classified")
    if not isinstance(sources, list) or not sources:
        errors.append("at least one source must be selected")
        sources = []
    else:
        unknown = [source for source in sources if source not in SOURCE_ADAPTERS]
        if unknown:
            errors.append("unsupported source(s): " + ", ".join(map(str, unknown)))
        sources = list(dict.fromkeys(source for source in sources if source in SOURCE_ADAPTERS))
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        errors.append("limit must be a positive integer")
        limit = 0
    if cities is not None:
        if not isinstance(cities, list) or not cities:
            errors.append("cities must be omitted or contain one or more exact supported labels")
            cities = []
        else:
            cities = list(dict.fromkeys(str(city) for city in cities))
            unknown = [city for city in cities if city not in ALL_CITIES]
            if unknown:
                errors.append("unsupported city/area label(s): " + ", ".join(unknown)
                              + "; supported labels: " + ", ".join(ALL_CITIES))
            explicit_sources = bool(raw.get("_source_explicit", False))
            if explicit_sources:
                for source in sources:
                    unsupported = [city for city in cities if city in ALL_CITIES
                                   and city not in SOURCE_CITIES[source]]
                    if unsupported:
                        errors.append(f"source {source!r} has no configured search for: "
                                      + ", ".join(unsupported))
            else:
                sources = [source for source in sources
                           if all(city not in ALL_CITIES or city in SOURCE_CITIES[source]
                                  for city in cities)]
                if not sources and not unknown:
                    errors.append("no configured source supports all requested city/area labels")
    normalized = {
        "country": country,
        "cities": cities,
        "intent": intent,
        "sources": sources,
        "limit": limit,
        "output": str(raw.get("output") or "output/leadforge_manifest.csv"),
        "run_id": str(raw.get("run_id") or "").strip(),
        "collect": bool(raw.get("collect", False)),
        "authorization_ref": str(raw.get("authorization_ref") or "").strip(),
        "_source_explicit": bool(raw.get("_source_explicit", False)),
    }
    if normalized["collect"] and not normalized["authorization_ref"]:
        errors.append("--collect requires a non-empty --authorization-ref")
    if not normalized["run_id"] and not errors:
        normalized["run_id"] = stable_run_id(normalized)
    return normalized, errors


def normalize_lead(record: dict, source_key: str, run_id: str) -> dict:
    """Map adapter fields into legacy and preview-import-compatible columns."""
    row = {field: record.get(field, "") for field in LEAD_FIELDS}
    row["source"] = row["source"] or SOURCE_ADAPTERS[source_key][0]
    if not row["id"]:
        row["id"] = hashlib.sha256(
            (row["source"] + "\0" + (row["link"] or row["title"])).encode("utf-8")
        ).hexdigest()[:12]
    row["run_id"] = run_id
    # The configured adapters only search owner/FSBO paths in Florida and do
    # not reliably classify property type. Preserve that as an unverified
    # source claim, never as independent owner verification.
    row.update({
        "source_name": row["source"],
        "source_url": row["link"],
        "external_id": row["id"],
        "country_code": "US",
        "region": "FL",
        "currency": "USD",
        "listing_type": "sale",
        "property_type": "other",
        "poster_type": "OWNER_UNVERIFIED",
        "poster_evidence": "Result came from a configured owner/FSBO search adapter; contact and owner identity are not independently verified.",
        "published_at": row["date_posted"],
    })
    return row


def write_manifest(output_path: str, records: list[dict], run_id: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=LEADFORGE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row = {field: record.get(field, "") for field in LEADFORGE_FIELDS}
            row["run_id"] = run_id
            writer.writerow(row)


def _real_adapters() -> tuple[dict, object]:
    """Create live browser adapter closures; called only after the auth gate."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is unavailable; install worker requirements first") from exc
    manager = sync_playwright()
    playwright = manager.start()
    browser, page = scraper.make_browser(playwright)
    os.makedirs(scraper.DEBUG_DIR, exist_ok=True)
    return {
        key: (lambda cities, limit, fn=adapter[1]: fn(page, cities=cities, limit=limit))
        for key, adapter in SOURCE_ADAPTERS.items()
    }, (manager, browser)


def run_worker(raw_config: dict, adapters: dict | None = None,
               event_sink: Callable[[dict], None] | None = None) -> tuple[str, list[dict], str]:
    """Run with injectable source functions, enabling entirely offline tests."""
    emit = event_sink or (lambda _event: None)
    config, errors = validate_config(raw_config)
    run_id = config.get("run_id") or "run-invalid"
    if errors:
        emit({"event": "run_validation_failed", "run_id": run_id,
              "status": "error", "errors": errors})
        return "error", [], run_id
    if not config["collect"]:
        emit({"event": "run_dry_run", "run_id": run_id, "status": "dry_run",
              "sources": config["sources"], "cities": config["cities"],
              "limit": config["limit"], "network_collection": False})
        return "dry_run", [], run_id

    emit({"event": "run_started", "run_id": run_id, "status": "running",
          "sources": config["sources"], "limit": config["limit"],
          "authorization_reference_supplied": True})
    manager_browser = None
    collected = []
    status = "completed"
    try:
        if adapters is None:
            adapters, manager_browser = _real_adapters()
        for source_key in config["sources"]:
            remaining = config["limit"] - len(collected)
            if remaining <= 0:
                break
            eligible_cities = config["cities"]
            if eligible_cities is not None:
                eligible_cities = [city for city in eligible_cities
                                   if city in SOURCE_CITIES[source_key]]
                if not eligible_cities:
                    continue
            emit({"event": "source_started", "run_id": run_id,
                  "source": source_key, "status": "running",
                  "completed": len(collected), "limit": config["limit"]})
            try:
                # Legacy adapter diagnostics may print; keep worker stdout JSONL-only.
                diagnostics = io.StringIO()
                with contextlib.redirect_stdout(diagnostics):
                    records = adapters[source_key](cities=eligible_cities, limit=remaining)
                if not isinstance(records, list):
                    raise TypeError("adapter returned a non-list result")
                rows = [normalize_lead(record, source_key, run_id) for record in records[:remaining]]
                collected.extend(rows)
                emit({"event": "source_progress", "run_id": run_id,
                      "source": source_key, "status": "completed",
                      "source_count": len(rows), "completed": len(collected),
                      "limit": config["limit"]})
                adapter_errors = [line.strip() for line in diagnostics.getvalue().splitlines()
                                  if "[!]" in line]
                if adapter_errors:
                    status = "error"
                    emit({"event": "source_error", "run_id": run_id,
                          "source": source_key, "status": "error",
                          "errors": adapter_errors})
                    break
            except Exception as exc:
                status = "error"
                emit({"event": "source_error", "run_id": run_id,
                      "source": source_key, "status": "error",
                      "error": f"{type(exc).__name__}: {exc}"})
                break
    except Exception as exc:
        status = "error"
        emit({"event": "run_error", "run_id": run_id, "status": "error",
              "error": f"{type(exc).__name__}: {exc}"})
    finally:
        if manager_browser is not None:
            manager, browser = manager_browser
            try:
                browser.close()
            finally:
                manager.stop()

    try:
        write_manifest(config["output"], collected, run_id)
    except Exception as exc:
        status = "error"
        emit({"event": "output_error", "run_id": run_id, "status": "error",
              "error": f"{type(exc).__name__}: {exc}"})
    emit({"event": "run_" + status, "run_id": run_id, "status": status,
          "count": len(collected), "output": config["output"]})
    return status, collected, run_id


def _event_printer(status_stream):
    def emit(event: dict) -> None:
        line = json.dumps(event, sort_keys=True, ensure_ascii=False)
        print(line, flush=True)
        if status_stream:
            status_stream.write(line + "\n")
            status_stream.flush()
    return emit


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="LeadForge listing worker; network collection is opt-in")
    parser.add_argument("--source", choices=sorted(SOURCE_ADAPTERS), action="append",
                        help="Repeat for multiple sources; default is all compatible sources")
    parser.add_argument("--country", default="US", help="Only US is configured")
    parser.add_argument("--city", action="append", help="Exact configured city/area label; repeatable")
    parser.add_argument("--intent", default="sale", help="Only sale is reliably supported")
    parser.add_argument("--property-type", help="Rejected: unsupported by these adapters")
    parser.add_argument("--limit", "--max-results", type=int, default=100, help="Maximum emitted listings")
    parser.add_argument("--output", default="output/leadforge_manifest.csv")
    parser.add_argument("--run-id", default="", help="Optional caller-provided run ID")
    parser.add_argument("--collect", action="store_true", help="Enable network collection")
    parser.add_argument("--authorization-ref", default="",
                        help="Required reference confirming permission to collect from selected sources")
    parser.add_argument("--status-file", default="", help="Optional JSON-lines run status log")
    args = parser.parse_args(argv)
    raw = {"country": args.country, "cities": args.city, "intent": args.intent,
           "sources": args.source or list(SOURCE_ADAPTERS), "limit": args.limit,
           "output": args.output, "run_id": args.run_id, "collect": args.collect,
           "authorization_ref": args.authorization_ref, "property_type": args.property_type,
           "_source_explicit": args.source is not None}
    status_stream = None
    try:
        if args.status_file:
            path = Path(args.status_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            status_stream = path.open("a", encoding="utf-8")
        emit = _event_printer(status_stream)
        status, _records, _run_id = run_worker(raw, event_sink=emit)
        return 0 if status in ("completed", "dry_run") else 2
    finally:
        if status_stream:
            status_stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
