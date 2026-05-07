#!/usr/bin/env python3
"""Scheduler proxy that calls the IAP-protected pROAS sync endpoint."""

from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)


def metadata_access_token() -> str:
    req = urllib.request.Request(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))["access_token"]
    except urllib.error.URLError as exc:
        raise RuntimeError("metadata_unavailable") from exc


def generate_id_token(service_account: str, audience: str) -> str:
    body = json.dumps({"audience": audience, "includeEmail": True}).encode("utf-8")
    url = f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{service_account}:generateIdToken"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {metadata_access_token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))["token"]


def token_matches(provided: str | None, expected: str | None) -> bool:
    if not expected:
        return False
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def http_error_payload(exc: urllib.error.HTTPError) -> dict[str, object]:
    try:
        text = exc.read().decode("utf-8", errors="replace")
    except Exception:
        text = ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {
            "error": "upstream",
            "status_code": exc.code,
            "body_preview": text[:500],
        }
    if isinstance(parsed, dict):
        parsed.setdefault("status_code", exc.code)
        return parsed
    return {"error": "upstream", "status_code": exc.code, "body_preview": str(parsed)[:500]}


def log_request(status: int, target: str | None, started: float) -> None:
    print(
        json.dumps(
            {
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "status": int(status),
                "target": target,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            },
            sort_keys=True,
        ),
        flush=True,
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.respond(HTTPStatus.OK, {"status": "ok"})
            return
        self.respond(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        started = time.perf_counter()
        target_url = os.environ.get("TARGET_SYNC_URL")
        status = HTTPStatus.INTERNAL_SERVER_ERROR
        if self.path != "/run":
            status = HTTPStatus.NOT_FOUND
            self.respond(status, {"error": "not found"})
            log_request(status, target_url, started)
            return
        expected = os.environ.get("SCHEDULER_PROXY_TOKEN")
        provided = self.headers.get("X-Scheduler-Token")
        if not expected:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            self.respond(status, {"error": "missing scheduler proxy token"})
            log_request(status, target_url, started)
            return
        if not token_matches(provided, expected):
            status = HTTPStatus.UNAUTHORIZED
            self.respond(status, {"error": "unauthorized"})
            log_request(status, target_url, started)
            return
        try:
            if not target_url:
                raise RuntimeError("missing TARGET_SYNC_URL")
            audience = os.environ["IAP_OAUTH_CLIENT_ID"]
            signing_sa = os.environ["IAP_SIGNING_SERVICE_ACCOUNT"]
            sync_token = os.environ["MMPRED_SYNC_TOKEN"]
            id_token = generate_id_token(signing_sa, audience)
            req = urllib.request.Request(
                target_url,
                headers={
                    "Authorization": f"Bearer {id_token}",
                    "X-Sync-Token": sync_token,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=600) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                status = HTTPStatus.OK if payload.get("status") == "ok" else HTTPStatus.BAD_GATEWAY
        except urllib.error.HTTPError as exc:
            payload = http_error_payload(exc)
            status = HTTPStatus.BAD_GATEWAY if exc.code >= 500 else exc.code
        except urllib.error.URLError as exc:
            payload = {"error": "network", "detail": str(exc.reason)}
            status = HTTPStatus.BAD_GATEWAY
        except RuntimeError as exc:
            error = str(exc)
            payload = {"error": error}
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        except Exception as exc:  # noqa: BLE001 - return JSON instead of stack trace to Scheduler.
            payload = {"error": "proxy_failure", "detail": str(exc)}
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self.respond(status, payload)
        log_request(status, target_url, started)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def respond(self, status: int, payload: object) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"pROAS scheduler proxy listening on :{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
