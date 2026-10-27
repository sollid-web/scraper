"""Private worker HTTP dispatch validation tests; no sockets or adapters are used."""
import unittest
import uuid

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
        }
        config.update(config_overrides)
        return {
            "runId": str(uuid.uuid4()),
            "callbackUrl": "https://app.example.test/api/scraper-runs/run/events",
            "configuration": config,
            "authorizationReference": "APPROVAL-123",
        }

    def test_valid_approved_dispatch_is_normalized_without_leaking_reference(self):
        body = self.request_body()
        dispatch, error = worker_api._validate_dispatch(body)
        self.assertIsNone(error)
        self.assertEqual(dispatch["run_id"], body["runId"])
        self.assertEqual(dispatch["normalized"]["sources"], ["forsalebyowner"])
        self.assertEqual(dispatch["normalized"]["cities"], ["Miami"])
        self.assertEqual(dispatch["normalized"]["intent"], "sale")
        self.assertTrue(dispatch["normalized"]["collect"])
        self.assertEqual(dispatch["normalized"]["authorization_ref"], "APPROVAL-123")
        self.assertTrue(dispatch["normalized"]["output"].endswith(body["runId"] + ".csv"))

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
