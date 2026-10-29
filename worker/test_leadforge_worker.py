"""Offline tests for worker orchestration; injected stubs only, never scraper adapters."""
import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import leadforge_worker as worker


class WorkerTests(unittest.TestCase):
    def test_manifest_headers_are_unique(self):
        self.assertEqual(len(worker.LEADFORGE_FIELDS), len(set(worker.LEADFORGE_FIELDS)))

    def base_config(self, output, **overrides):
        config = {
            "country": "US",
            "cities": ["Miami"],
            "intent": "sale",
            "sources": ["craigslist"],
            "limit": 2,
            "output": str(output),
            "collect": True,
            "authorization_ref": "TEST-AUTH-123",
            "_source_explicit": True,
        }
        config.update(overrides)
        return config

    def test_rejects_unsupported_country_intent_property_and_city_source(self):
        cfg, errors = worker.validate_config(self.base_config(
            "unused.csv", country="CA", intent="rent", property_type="condo",
            cities=["Miami", "Boca Raton"]))
        self.assertTrue(errors)
        self.assertTrue(any("country" in error for error in errors))
        self.assertTrue(any("intent" in error for error in errors))
        self.assertTrue(any("property type" in error for error in errors))
        self.assertTrue(any("Boca Raton" in error for error in errors))

    def test_rejects_missing_authorization_before_calling_source(self):
        called = []
        events = []
        cfg = self.base_config("unused.csv", authorization_ref="")
        status, records, run_id = worker.run_worker(
            cfg,
            adapters={"craigslist": lambda **_kw: called.append(True) or []},
            event_sink=events.append,
        )
        self.assertEqual(status, "error")
        self.assertEqual(records, [])
        self.assertFalse(called)
        self.assertEqual(events[-1]["event"], "run_validation_failed")
        self.assertTrue(any("authorization" in item for item in events[-1]["errors"]))
        self.assertTrue(run_id.startswith("run-"))

    def test_run_id_is_deterministic_and_explicit_id_wins(self):
        cfg_a, errors_a = worker.validate_config(self.base_config("one.csv"))
        cfg_b, errors_b = worker.validate_config(self.base_config("two.csv"))
        self.assertEqual(errors_a, [])
        self.assertEqual(errors_b, [])
        self.assertEqual(cfg_a["run_id"], cfg_b["run_id"])
        explicit, errors = worker.validate_config(self.base_config("one.csv", run_id="caller-run"))
        self.assertEqual(errors, [])
        self.assertEqual(explicit["run_id"], "caller-run")

    def test_normalized_listing_id_is_idempotent(self):
        sample = {"source": "Craigslist", "link": "https://example.invalid/listing/1",
                  "title": "Sample home"}
        first = worker.normalize_lead(sample, "craigslist", "run-a")
        second = worker.normalize_lead(sample, "craigslist", "run-b")
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_synthetic_records_produce_leadforge_manifest_schema(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "nested" / "manifest.csv"
            events = []
            def synthetic_adapter(cities, limit):
                self.assertEqual(cities, ["Miami"])
                self.assertEqual(limit, 2)
                return [{"title": "Synthetic listing", "link": "https://example.invalid/one",
                         "city": "Miami", "price": "$100000", "type": "FSBO"}]
            status, records, run_id = worker.run_worker(
                self.base_config(output), adapters={"craigslist": synthetic_adapter},
                event_sink=events.append)
            self.assertEqual(status, "completed")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["source"], "Craigslist")
            self.assertEqual(records[0]["run_id"], run_id)
            with output.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)
                self.assertEqual(reader.fieldnames, worker.LEADFORGE_FIELDS)
            self.assertEqual(rows[0]["title"], "Synthetic listing")
            self.assertTrue(rows[0]["id"])
            self.assertEqual(rows[0]["run_id"], run_id)
            self.assertEqual(rows[0]["country_code"], "US")
            self.assertEqual(rows[0]["listing_type"], "sale")
            self.assertEqual(rows[0]["currency"], "USD")
            self.assertEqual(rows[0]["property_type"], "other")
            self.assertEqual(rows[0]["poster_type"], "OWNER_UNVERIFIED")
            self.assertIn("human verification", rows[0]["poster_evidence"])
            self.assertEqual(rows[0]["source_url"], "https://example.invalid/one")
            self.assertEqual(events[0]["event"], "run_started")
            self.assertEqual(events[-1]["event"], "run_completed")

    def test_source_specific_owner_signal_is_only_an_unverified_candidate(self):
        for source in ("craigslist", "forsalebyowner", "byowner"):
            result = worker.scraper.classify_poster(source, "A home for sale")
            self.assertEqual(result["decision"], "owner_candidate")
            self.assertEqual(result["poster_type"], "OWNER_UNVERIFIED")
            self.assertIn("human verification", result["poster_evidence"])

    def test_agent_and_broker_evidence_rejects_candidate_even_on_owner_source(self):
        for source in ("craigslist", "forsalebyowner", "byowner"):
            result = worker.scraper.classify_poster(
                source, "Home for sale", "Listed by a licensed real estate agent at Compass Real Estate"
            )
            self.assertEqual(result["decision"], "reject_agent")
            self.assertEqual(result["poster_type"], "AGENT")

    def test_owner_no_agent_disclaimer_is_not_misclassified_as_agent(self):
        result = worker.scraper.classify_poster("craigslist", "Home for sale", "No agents please; owner selling directly.")
        self.assertEqual(result["decision"], "owner_candidate")

    def test_unknown_source_is_held_and_contact_fields_are_removed(self):
        result = worker.scraper.classify_poster("unknown", "Home for sale")
        self.assertEqual(result["decision"], "hold_ambiguous")
        record = {
            "source": "Unknown", "title": "Home for sale", "link": "https://example.invalid/1",
            "owner_name": "Seller Name", "email": "seller@example.invalid", "phone": "3055550100",
        }
        normalized = worker.normalize_lead(record, "craigslist", "run-a")
        # An allowlisted adapter still supplies source-level owner evidence, but
        # this direct test verifies no caller-provided contact leaks on a reject.
        rejected = worker.normalize_lead({**record, "poster_decision": "reject_agent"}, "craigslist", "run-a")
        self.assertEqual(normalized["poster_decision"], "owner_candidate")
        self.assertEqual(rejected["poster_decision"], "reject_agent")
        self.assertEqual(rejected["phone"], "")
        self.assertEqual(rejected["email"], "")
        self.assertEqual(rejected["owner_name"], "")

    def test_error_status_and_event_when_injected_adapter_fails(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "manifest.csv"
            events = []
            def broken_adapter(**_kwargs):
                raise RuntimeError("synthetic offline failure")
            status, records, _run_id = worker.run_worker(
                self.base_config(output), adapters={"craigslist": broken_adapter},
                event_sink=events.append)
            self.assertEqual(status, "error")
            self.assertEqual(records, [])
            self.assertTrue(output.exists())
            self.assertTrue(any(e["event"] == "source_error" and "synthetic offline failure" in e["error"]
                                for e in events))
            self.assertEqual(events[-1]["event"], "run_error")

    def test_adapter_printed_error_becomes_structured_run_error(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "manifest.csv"
            events = []
            def warning_adapter(**_kwargs):
                print("    [!] City error: synthetic offline failure")
                return []
            status, records, _run_id = worker.run_worker(
                self.base_config(output), adapters={"craigslist": warning_adapter},
                event_sink=events.append)
            self.assertEqual(status, "error")
            self.assertEqual(records, [])
            error_event = next(e for e in events if e["event"] == "source_error")
            self.assertIn("synthetic offline failure", error_event["errors"][0])
            self.assertEqual(events[-1]["event"], "run_error")

    def test_default_mode_is_dry_run_and_never_calls_adapter(self):
        events = []
        status, records, _run_id = worker.run_worker(
            {"country": "US", "sources": ["craigslist"], "limit": 1},
            adapters={"craigslist": lambda **_kw: self.fail("dry run invoked an adapter")},
            event_sink=events.append)
        self.assertEqual(status, "dry_run")
        self.assertEqual(records, [])
        self.assertEqual(events[0]["network_collection"], False)

    def test_cli_default_prints_jsonl_dry_run(self):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = worker.main(["--source", "craigslist", "--city", "Miami"])
        self.assertEqual(exit_code, 0)
        self.assertIn('"event": "run_dry_run"', output.getvalue())
        self.assertNotIn("authorization_ref", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
