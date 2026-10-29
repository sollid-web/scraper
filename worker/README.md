# LeadForge listings worker (isolated scraper clone)

This worker wraps the legacy adapters in the separate `scraper.py` clone. It does not connect directly to LeadForge's database; it reports through the authenticated app callback. It produces an offline-friendly JSON-lines run stream and a normalized CSV manifest with the scraper's existing lead columns plus `run_id` and poster-decision evidence.

## Safe defaults

The worker never collects automatically. Running `python3 worker/leadforge_worker.py --help` shows options; a normal invocation without `--collect` validates configuration and emits `event: run_dry_run`. It does not create a browser, contact a source, or write the CSV manifest. The Docker image starts an authenticated HTTP dispatcher; merely starting that API does not collect anything. HTTP dispatches are also offline dry-runs by default.

Network collection is only enabled by explicitly supplying **both** `--collect` and a non-empty `--authorization-ref` that identifies the user's permission to collect from the selected sources. The worker checks for the reference before it initializes Playwright. The reference value is deliberately not written to status output or CSV. Supplying a reference is an operator assertion, not an automated legal/terms check. Review source terms, applicable law, privacy obligations, and internal approvals before a collection run. Stop if a source denies access or presents a block; this worker does not attempt to bypass it.

Example dry run (safe, offline):

```sh
python3 worker/leadforge_worker.py \
  --source forsalebyowner --city Miami --country US --intent sale \
  --limit 20 --output output/manifest.csv --status-file output/run.jsonl
```

Explicit collection form (not run as part of this change):

```sh
python3 worker/leadforge_worker.py \
  --collect --authorization-ref TICKET-OR-APPROVAL-REFERENCE \
  --source forsalebyowner --city Miami --country US --intent sale \
  --limit 20 --output output/manifest.csv --status-file output/run.jsonl
```

`--source` and `--city` can be repeated. Supported adapters are `craigslist`, `forsalebyowner`, and `byowner`, with the exact LeadForge source names `Craigslist`, `ForSaleByOwner.com`, and `ByOwner.com`. The HTTP dispatcher accepts the legacy source-key slugs `forsalebyowner-com` and `byowner-com` and normalizes them to the canonical keys; it still requires the exact adapter name and matching HTTPS host. Country coverage is only `US`; configured search labels are Miami, Fort Lauderdale, West Palm Beach, Broward, Boca Raton, Hialeah, and Palm Beach. A label must have a configured search for the selected source. The source's labels include county/area names, not just municipalities. LeadForge dispatch additionally requires an active built-in source record and a separately recorded authorization policy for the exact collection scope.

Only `sale` is supported. The old selectors do not reliably classify rentals or property categories, so `--intent rent` and any non-empty `--property-type` fail validation rather than guessing. The result cap is applied across sources and inside each adapter. A shared classifier rejects explicit agent/broker language, recognizes only the configured source/search-path signal as an unverified owner candidate, and marks records with no strong configured signal as ambiguous. It never asserts independent owner verification. Rejected and held rows have their contact fields stripped before the manifest is written; the LeadForge hold queue stores no contacts. Text-based filtering is not proof of ownership and may have false positives/negatives, so test labeled source pages and retain human review. Each legacy source can still issue search/detail requests for items needed to build the capped result, so check with the source owner on permitted collection behavior before opting in.

## Run events and CSV

Stdout is JSON Lines, with run IDs and events such as `run_dry_run`, `run_started`, `source_started`, `source_progress`, `source_error`, `output_error`, `run_completed`, and `run_error`. `--status-file` optionally appends the same JSONL events to a file. A missing caller-supplied run ID is derived deterministically from country, cities, intent, sources, and result limit; the same normalized request therefore gets the same ID. A supplied `--run-id` is preserved.

The CSV manifest retains the legacy fields (`id`, `lead_type`, `type`, `source`, `city`, `title`, `price`, `beds_baths`, `location`, `date_posted`, `link`, `owner_name`, `phone`, `email`, `description`) and adds `run_id`, `poster_decision`, `poster_decision_reason`, and `poster_evidence` plus the LeadForge preview/import columns (`source_name`, `source_url`, `external_id`, `country_code`, `region`, `currency`, `listing_type`, `property_type`, `poster_type`, `published_at`). `poster_decision` is `owner_candidate`, `reject_agent`, or `hold_ambiguous`; only `owner_candidate` may proceed to owner verification. Empty results still produce a header row after an opted-in run. Worker errors produce a non-zero exit status and a terminal error event; if any records were already collected, they are written as a partial manifest.

## Private dispatcher API

`worker/worker_api.py` exposes `POST /runs`, `GET /runs/<uuid>/artifact`, and a token-protected `GET /health`. The dispatcher requires `LEADFORGE_WORKER_TOKEN` to be a random secret of at least 32 characters and accepts only one run at a time. It validates the app-provided source adapter, exact source display name/host, supported city, US sale intent, unfiltered property type, result cap, HTTPS callback, source approval flags, and non-empty authorization reference. Network collection requires both `configuration.liveCollectionEnabled=true` from LeadForge and `LEADFORGE_WORKER_LIVE_COLLECTION_ENABLED=true` in the worker environment; either missing/false switch performs an offline dry-run and callback instead. The worker-side switch defaults to false. The token is sent back to the LeadForge callback. Status callbacks contain only bounded progress/counts and errors, never contact details. Artifacts are written under `LEADFORGE_ARTIFACT_DIR` (default `/data/artifacts`) with restrictive directory/file permissions and are streamed only after token authorization.

Run this API behind TLS or on a private network with TLS termination. Do not expose the worker port directly to the public internet; protect the token in a secret manager and mount a private persistent artifact directory with an explicit retention/deletion policy. Set the LeadForge app's `LEADFORGE_SCRAPER_WORKER_URL` to the HTTPS `/runs` endpoint and `LEADFORGE_SCRAPER_WORKER_TOKEN` to the same secret. In LeadForge, scheduled callbacks automatically validate and ingest completed manifests; owner/contact identity remains unverified until a manager reviews it. Keep both live switches off until staging permissions and dry-run callbacks are verified. The worker container image has not been built or started as part of this change.

## Historical-code caveats

This is a historical clone at commit `34504e1`, not a verified current production scraper. The existing selector logic, city URLs, source coverage, and classification heuristics may be stale or inaccurate. The repository also contains old output CSVs and debug HTML/images, including Zillow-named artifacts even though Zillow is not one of the three source adapters wired into this worker. Those files are historical artifacts, not evidence of supported current sources or successful collection. Validate source ownership and coverage independently before any authorized run.

The scraper's original browser helper used automation-disguise flags, randomized fingerprints, webdriver masking, and ignored HTTPS errors. This change removes those behaviors from `scraper.py`'s browser helper and replaces randomized pacing/scrolling with ordinary fixed pauses. The unrelated legacy `enrich.py` still contains browser fingerprint spoofing and `ignore_https_errors=True`; `skip_trace.py` can initiate paid third-party API calls and even attempts to install a missing dependency at import time. Neither script is part of this worker, and neither was invoked or modified. Do not treat this worker as permission to run enrichment, skip tracing, setup scripts, or other historical utilities.

## Container

From the repository root, build with `docker build -f worker/Dockerfile -t leadforge-scraper-worker .`. Starting the API requires a token and makes no source request. For an offline CLI dry run, override the command with `python3 /app/worker/leadforge_worker.py --source forsalebyowner --city Miami --country US --intent sale --limit 20`. For a persistent dispatcher, mount a restricted artifact volume and provide the token via your deployment secret manager. The Docker image has not been built or started as part of this change.

## Offline tests

Run from the repository root:

```sh
python3 -m unittest discover -s worker -p 'test_*.py' -v
```

Tests inject synthetic source functions and never invoke scraper adapters, Playwright, enrichment, skip tracing, or external services.
