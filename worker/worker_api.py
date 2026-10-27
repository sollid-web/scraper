#!/usr/bin/env python3
"""Private HTTP dispatcher for explicitly authorized LeadForge worker runs.

Starting this process only opens an authenticated API. It never starts a scrape
until a manager-configured application submits a signed bearer-token request.
"""
from __future__ import annotations

import concurrent.futures
import hmac
import json
import os
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import leadforge_worker as worker

TOKEN = os.environ.get("LEADFORGE_WORKER_TOKEN", "")
ARTIFACT_DIR = Path(os.environ.get("LEADFORGE_ARTIFACT_DIR", "/data/artifacts")).resolve()
SOURCE_HOSTS = {
    "craigslist": "craigslist.org",
    "forsalebyowner": "forsalebyowner.com",
    "byowner": "byowner.com",
}
CITY_LABELS = {"Miami", "Fort Lauderdale", "West Palm Beach", "Broward", "Boca Raton", "Hialeah", "Palm Beach"}
MAX_REQUEST_BYTES = 64 * 1024
LOCK = threading.Lock()
ACTIVE_RUN: str | None = None
EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="leadforge-scrape")


def _auth_ok(handler: BaseHTTPRequestHandler) -> bool:
    supplied = handler.headers.get("Authorization", "")
    if not supplied.lower().startswith("bearer ") or not TOKEN:
        return False
    return hmac.compare_digest(supplied[7:].encode(), TOKEN.encode())


def _host_matches(host: str, expected_suffix: str) -> bool:
    hostname = host.lower().rstrip(".")
    return hostname == expected_suffix or hostname.endswith("." + expected_suffix)


def _validate_dispatch(body: dict) -> tuple[dict | None, str | None]:
    try:
        run_id = str(uuid.UUID(str(body.get("runId", ""))))
    except (ValueError, TypeError, AttributeError):
        return None, "runId must be a UUID"
    callback = body.get("callbackUrl")
    if not isinstance(callback, str):
        return None, "callbackUrl must use HTTPS"
    callback_url = urlsplit(callback)
    if callback_url.scheme != "https" or not callback_url.hostname or callback_url.username or callback_url.password or callback_url.fragment:
        return None, "callbackUrl must be a credential-free HTTPS URL"
    config = body.get("configuration")
    if not isinstance(config, dict):
        return None, "configuration must be an object"
    if config.get("countryCode") != "US" or config.get("listingIntent") != "sale":
        return None, "only configured US sale searches are supported"
    if config.get("propertyType") != "all":
        return None, "this adapter cannot reliably filter property type"
    if config.get("sourceType") not in {"approved_feed", "partner_feed"} or config.get("sourceApproved") is not True or config.get("sourceActive") is not True or config.get("authorizationConfirmed") is not True:
        return None, "an approved, active source and explicit operator authorization are required"
    source_key = config.get("sourceKey")
    expected_host = SOURCE_HOSTS.get(source_key)
    if not expected_host:
        return None, "unsupported source adapter"
    source_url = config.get("sourceUrl")
    if not isinstance(source_url, str):
        return None, "approved source URL is required"
    parsed_source = urlsplit(source_url)
    if parsed_source.scheme != "https" or not parsed_source.hostname or parsed_source.username or parsed_source.password or parsed_source.query or parsed_source.fragment or not _host_matches(parsed_source.hostname, expected_host):
        return None, "source URL host does not match the selected adapter"
    if config.get("sourceName") != worker.SOURCE_ADAPTERS[source_key][0]:
        return None, "LeadForge source name must match the configured adapter name"
    city = config.get("city")
    if city not in CITY_LABELS:
        return None, "unsupported exact city label"
    limit = config.get("resultLimit")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        return None, "resultLimit must be between 1 and 50"
    authorization_reference = body.get("authorizationReference")
    if not isinstance(authorization_reference, str) or len(authorization_reference.strip()) < 6 or len(authorization_reference) > 500:
        return None, "authorizationReference is required"
    raw = {
        "country": "US",
        "cities": [city],
        "intent": "sale",
        "sources": [source_key],
        "limit": limit,
        "output": str(ARTIFACT_DIR / f"{run_id}.csv"),
        "run_id": run_id,
        "collect": True,
        "authorization_ref": authorization_reference,
        "_source_explicit": True,
    }
    normalized, errors = worker.validate_config(raw)
    if errors:
        return None, "; ".join(errors)
    return {"run_id": run_id, "callback_url": callback, "normalized": normalized}, None


def _post_event(callback_url: str, event: dict) -> None:
    data = json.dumps(event, separators=(",", ":")).encode("utf-8")
    request = Request(callback_url, data=data, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=8) as response:
            if response.status >= 300:
                print(json.dumps({"event": "callback_error", "status": response.status}), file=sys.stderr, flush=True)
    except (OSError, URLError, TimeoutError) as exc:
        print(json.dumps({"event": "callback_error", "error_type": type(exc).__name__}), file=sys.stderr, flush=True)


def _safe_progress(message: str) -> str:
    message = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[email redacted]", message, flags=re.I)
    message = re.sub(r"\+?\d[\d ().-]{7,}\d", "[phone redacted]", message)
    message = re.sub(r"https?://\S+", "[url redacted]", message)
    return message[:200]


def _run_dispatch(dispatch: dict) -> None:
    global ACTIVE_RUN
    run_id = dispatch["run_id"]
    callback_url = dispatch["callback_url"]
    config = dispatch["normalized"]
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(ARTIFACT_DIR, 0o700)
    except OSError:
        pass

    def emit(event: dict) -> None:
        name = event.get("event", "progress")
        count = int(event.get("count", event.get("completed", 0)) or 0)
        if name in {"run_completed", "run_error"}:
            return
        progress_stage = name.replace("run_", "").replace("source_", "source_")[:80]
        message = "Worker is processing the approved source" if name in {"source_started", "source_progress"} else "Worker run started"
        _post_event(callback_url, {
            "event": "progress",
            "progress": {"stage": progress_stage, "message": _safe_progress(message),
                         "pagesProcessed": 0, "rowsSeen": count},
            "counts": {"discovered": count, "imported": 0, "duplicates": 0, "rejected": 0, "contactsFound": 0},
        })

    try:
        # The CLI worker emits no owner-contact values in events; records stay
        # only in the private CSV artifact until a manager downloads it.
        status, records, _worker_run_id = worker.run_worker(config, event_sink=emit)
        artifact = ARTIFACT_DIR / f"{run_id}.csv"
        if artifact.is_file():
            try:
                artifact.chmod(0o600)
            except OSError:
                pass
        count = len(records)
        contacts_found = sum(bool(record.get("email") or record.get("phone")) for record in records)
        has_artifact = artifact.is_file()
        if status == "completed" and has_artifact:
            _post_event(callback_url, {
                "event": "completed",
                "progress": {"stage": "completed", "message": "Manifest ready for manager preview", "rowsSeen": count},
                "counts": {"discovered": count, "imported": 0, "duplicates": 0, "rejected": 0, "contactsFound": contacts_found},
                "artifactName": artifact.name,
            })
        elif status == "error" and count > 0 and has_artifact:
            _post_event(callback_url, {
                "event": "degraded",
                "progress": {"stage": "partial_error", "message": "Worker stopped with a partial manifest; review before import", "rowsSeen": count},
                "counts": {"discovered": count, "imported": 0, "duplicates": 0, "rejected": 0, "contactsFound": contacts_found},
                "errorMessage": "A source or output error stopped the worker; the partial manifest requires review.",
                "artifactName": artifact.name,
            })
        elif status == "error":
            _post_event(callback_url, {
                "event": "failed", "progress": {"stage": "failed", "message": "Worker failed before producing an importable manifest", "rowsSeen": count},
                "counts": {"discovered": count, "imported": 0, "duplicates": 0, "rejected": 0, "contactsFound": contacts_found},
                "errorMessage": "Worker failed before producing an importable manifest.",
            })
        else:
            _post_event(callback_url, {
                "event": "failed", "progress": {"stage": "failed", "message": "Worker did not enter an explicitly authorized collection mode", "rowsSeen": count},
                "counts": {"discovered": count, "imported": 0, "duplicates": 0, "rejected": 0, "contactsFound": contacts_found},
                "errorMessage": "Worker did not enter the expected authorized collection mode.",
            })
    except Exception as exc:  # Defensive boundary: never leak arbitrary errors to app.
        print(json.dumps({"event": "dispatcher_error", "type": type(exc).__name__}), file=sys.stderr, flush=True)
        _post_event(callback_url, {"event": "failed", "progress": {"stage": "failed", "message": "Worker dispatcher failed safely"}, "errorMessage": "Worker dispatcher failed safely."})
    finally:
        with LOCK:
            if ACTIVE_RUN == run_id:
                ACTIVE_RUN = None


class Handler(BaseHTTPRequestHandler):
    server_version = "LeadForgeWorker/1.0"

    def log_message(self, format_string, *args):
        # Avoid default access logs containing potentially sensitive query text.
        sys.stderr.write("LeadForgeWorker request\n")

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        global ACTIVE_RUN
        if urlsplit(self.path).path != "/runs":
            self._json(404, {"error": "not found"})
            return
        if not _auth_ok(self):
            self._json(401, {"error": "unauthorized"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = MAX_REQUEST_BYTES + 1
        if size <= 0 or size > MAX_REQUEST_BYTES:
            self._json(413, {"error": "request body size rejected"})
            return
        try:
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("object required")
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            self._json(400, {"error": "invalid JSON object"})
            return
        dispatch, error = _validate_dispatch(body)
        if error:
            self._json(400, {"error": error})
            return
        with LOCK:
            if ACTIVE_RUN:
                self._json(429, {"error": "worker is busy; do not queue stale collection requests"})
                return
            run_id = dispatch["run_id"]
            artifact = ARTIFACT_DIR / f"{run_id}.csv"
            if artifact.exists():
                self._json(409, {"error": "artifact for this run ID already exists"})
                return
            ACTIVE_RUN = run_id
        try:
            EXECUTOR.submit(_run_dispatch, dispatch)
        except RuntimeError:
            with LOCK:
                ACTIVE_RUN = None
            self._json(503, {"error": "worker executor unavailable"})
            return
        self._json(202, {"runId": run_id, "accepted": True})

    def do_GET(self):
        if urlsplit(self.path).path == "/health":
            if not _auth_ok(self):
                self._json(401, {"error": "unauthorized"})
                return
            with LOCK:
                busy = ACTIVE_RUN is not None
            self._json(200, {"status": "ok", "busy": busy})
            return
        match = re.fullmatch(r"/runs/([0-9a-fA-F-]{36})/artifact", urlsplit(self.path).path)
        if not match:
            self._json(404, {"error": "not found"})
            return
        if not _auth_ok(self):
            self._json(401, {"error": "unauthorized"})
            return
        try:
            run_id = str(uuid.UUID(match.group(1)))
        except ValueError:
            self._json(404, {"error": "not found"})
            return
        artifact = (ARTIFACT_DIR / f"{run_id}.csv").resolve()
        if artifact.parent != ARTIFACT_DIR or not artifact.is_file():
            self._json(404, {"error": "artifact not found"})
            return
        try:
            payload = artifact.read_bytes()
        except OSError:
            self._json(500, {"error": "artifact unavailable"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{run_id}.csv"')
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    if len(TOKEN) < 32:
        print("LEADFORGE_WORKER_TOKEN must be a randomly generated secret of at least 32 characters", file=sys.stderr)
        return 2
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(ARTIFACT_DIR, 0o700)
    except OSError:
        pass
    host = os.environ.get("LEADFORGE_WORKER_HOST", "0.0.0.0")
    port = int(os.environ.get("LEADFORGE_WORKER_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(json.dumps({"event": "worker_api_ready", "host": host, "port": port}), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        EXECUTOR.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
