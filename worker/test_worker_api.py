"""Private worker HTTP dispatch validation tests; no sockets or adapters are used."""
import unittest
import uuid
from unittest.mock import patch

import worker_api


class WorkerApiValidationTests(unittest.TestCase):
    def request_body(self, **config_overrides):
        config = {
            "sourceKey": "forsalebyowner",
            "sourceId": str(uuid.uuid4()),
            "sourceName": "ForSaleByOwner.com",
            "sourceUrl": "https://www.forsalebyowner.com/",
            "sourceType": "approved_feed",
            "sourceApproved": True,
            "sourceActive": True,
            "authorizationConfirmed": True,
            "countryCode": "US",
            "city": "Miami",
            "propertyType": "all",
            "listingIntent": "sale",
            "resultLimit": 25,
            "liveCollectionEnabled": False,
        }
        config.update(config_overrides)
        return {
            "runId": str(uuid.uuid4()),
            "callbackUrl": "https://app.example.test/api/scraper-runs/run/events",
            "configuration": config,
            "authorizationReference": "APPROVAL-123",
        }

    def test_valid_approved_dispatch_is_normalized_without_leaking_reference(self):
        body = self.request_body(liveCollectionEnabled=True)
        with patch.dict(worker_api.os.environ, {"LEADFORGE_WORKER_LIVE_COLLECTION_ENABLED": "true"}):
            dispatch, error = worker_api._validate_dispatch(body)
        self.assertIsNone(error)
        self.assertEqual(dispatch["run_id"], body["runId"])
        self.assertEqual(dispatch["normalized"]["sources"], ["forsalebyowner"])
        self.assertEqual(dispatch["normalized"]["cities"], ["Miami"])
        self.assertEqual(dispatch["normalized"]["intent"], "sale")
        self.assertTrue(dispatch["normalized"]["collect"])
        self.assertEqual(dispatch["normalized"]["authorization_ref"], "APPROVAL-123")
        self.assertTrue(dispatch["normalized"]["output"].endswith(body["runId"] + ".csv"))

    def test_canonical_keys_and_slug_suffix_aliases_normalize_to_worker_keys(self):
        cases = [
            ("forsalebyowner", "forsalebyowner", "ForSaleByOwner.com", "https://www.forsalebyowner.com/"),
            ("forsalebyowner-com", "forsalebyowner", "ForSaleByOwner.com", "https://www.forsalebyowner.com/"),
            ("byowner", "byowner", "ByOwner.com", "https://www.byowner.com/"),
            ("byowner-com", "byowner", "ByOwner.com", "https://www.byowner.com/"),
        ]
        for alias, canonical, display_name, source_url in cases:
            with self.subTest(alias=alias):
                body = self.request_body(
                    sourceKey=alias,
                    sourceName=display_name,
                    sourceUrl=source_url,
                )
                dispatch, error = worker_api._validate_dispatch(body)
                self.assertIsNone(error)
                self.assertEqual(dispatch["normalized"]["sources"], [canonical])
                self.assertFalse(dispatch["normalized"]["collect"])

    def test_slug_suffix_aliases_still_require_exact_host_and_display_name(self):
        cases = [
            (
                "forsalebyowner-com", "ForSaleByOwner.com",
                "https://www.forsalebyowner.com/", "https://evil.example/",
            ),
            (
                "byowner-com", "ByOwner.com",
                "https://www.byowner.com/", "https://evil.example/",
            ),
        ]
        for alias, display_name, valid_url, invalid_url in cases:
            with self.subTest(alias=alias, mismatch="host"):
                _dispatch, error = worker_api._validate_dispatch(self.request_body(
                    sourceKey=alias, sourceName=display_name, sourceUrl=invalid_url,
                ))
                self.assertIn("host does not match", error)
            with self.subTest(alias=alias, mismatch="name"):
                _dispatch, error = worker_api._validate_dispatch(self.request_body(
                    sourceKey=alias, sourceName="Some feed", sourceUrl=valid_url,
                ))
                self.assertIn("source name must match", error)

    def test_dry_run_is_the_default_even_with_authorization_reference(self):
        body = self.request_body()
        with patch.dict(worker_api.os.environ, {"LEADFORGE_WORKER_LIVE_COLLECTION_ENABLED": "false"}):
            dispatch, error = worker_api._validate_dispatch(body)
        self.assertIsNone(error)
        self.assertFalse(dispatch["normalized"]["collect"])

    def test_live_collection_requires_both_app_and_worker_switches(self):
        cases = [
            (False, "true", False),
            (True, "false", False),
            (True, "true", True),
        ]
        for requested_live, worker_flag, expected in cases:
            with self.subTest(requested_live=requested_live, worker_flag=worker_flag):
                body = self.request_body(liveCollectionEnabled=requested_live)
                with patch.dict(worker_api.os.environ, {"LEADFORGE_WORKER_LIVE_COLLECTION_ENABLED": worker_flag}):
                    dispatch, error = worker_api._validate_dispatch(body)
                self.assertIsNone(error)
                self.assertEqual(dispatch["normalized"]["collect"], expected)

    def test_rejects_unapproved_source_or_missing_confirmation(self):
        for changes in (
            {"sourceApproved": False}, {"sourceActive": False},
            {"sourceType": "manual"}, {"authorizationConfirmed": False},
        ):
            with self.subTest(changes=changes):
                _dispatch, error = worker_api._validate_dispatch(self.request_body(**changes))
                self.assertIn("approved, active source", error)

    def test_rejects_source_host_adapter_name_market_and_intent_mismatches(self):
        cases = [
            ({"sourceUrl": "https://evil.example/"}, "host does not match"),
            ({"sourceKey": "craigslist"}, "host does not match"),
            ({"sourceName": "Some feed"}, "source name must match"),
            ({"city": "Orlando"}, "unsupported exact city"),
            ({"city": "Palm Beach"}, "no configured search"),
            ({"listingIntent": "rental"}, "only configured US sale"),
            ({"propertyType": "house"}, "cannot reliably filter"),
            ({"resultLimit": 51}, "between 1 and 50"),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                _dispatch, error = worker_api._validate_dispatch(self.request_body(**changes))
                self.assertIn(expected, error)

    def test_requires_https_callback_and_authorization_reference(self):
        body = self.request_body()
        body["callbackUrl"] = "http://127.0.0.1:3000/callback"
        _dispatch, error = worker_api._validate_dispatch(body)
        self.assertEqual(error, "callbackUrl must be a credential-free HTTPS URL")
        body = self.request_body()
        body["authorizationReference"] = ""
        _dispatch, error = worker_api._validate_dispatch(body)
        self.assertEqual(error, "authorizationReference is required")


if __name__ == "__main__":
    unittest.main(verbosity=2)
