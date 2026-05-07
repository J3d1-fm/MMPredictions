#!/usr/bin/env python3
"""Small Cloud Run web app for MMPredictions pROAS predictions."""

from __future__ import annotations

import datetime as dt
import email.utils
import hmac
import json
import mimetypes
import os
import pathlib
import threading
import time
import traceback
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from mmpredictions import engine, gcs_store


CONFIG = engine.load_config()
STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
SYNC_TOKEN = os.environ.get("MMPRED_SYNC_TOKEN")
ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.environ.get("MMPRED_ADMIN_EMAILS", "").split(",")
    if email.strip()
}
IAP_RESOURCE = os.environ.get("MMPRED_IAP_RESOURCE", "")
IAP_ACCESS_ROLE = "roles/iap.httpsResourceAccessor"
IAP_POLICY_LOCK = threading.Lock()
ADMIN_STORE_PATH = "access/admins.json"

SYNC_LOCK = threading.Lock()
SEED_TRIGGER_LOCK = threading.Lock()
SYNC_IN_PROGRESS = False
SYNC_STARTED_AT: str | None = None
SUMMARY_LOCK = threading.Lock()
SUMMARY_CACHE: dict[str, object] = {"payloads": {}}
SUMMARY_TTL_SECONDS = 60.0


def json_bytes(payload: object, status: int = 200) -> tuple[int, bytes, str]:
    return status, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8"


def html_bytes() -> tuple[int, bytes, str]:
    return HTTPStatus.OK, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8"


def token_matches(provided: str | None, expected: str | None) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def normalize_email(value: str | None) -> str:
    raw = (value or "").strip()
    if ":" in raw:
        raw = raw.rsplit(":", 1)[-1]
    return raw.lower()


def valid_email(value: str) -> bool:
    parsed = email.utils.parseaddr(value)[1]
    return parsed == value and "@" in parsed and "." in parsed.rsplit("@", 1)[-1]


def current_user_email(headers) -> str:
    return normalize_email(headers.get("X-Goog-Authenticated-User-Email") or headers.get("X-User-Email"))


def stored_admin_emails() -> set[str]:
    payload = gcs_store.download_gzip_json(CONFIG, ADMIN_STORE_PATH) if gcs_store.enabled(CONFIG) else None
    admins = payload.get("admins", []) if isinstance(payload, dict) else []
    return {normalize_email(str(email)) for email in admins if normalize_email(str(email))}


def write_stored_admin_emails(admins: set[str]) -> None:
    if not gcs_store.enabled(CONFIG):
        return
    payload = {
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "admins": sorted(admins),
    }
    gcs_store.upload_gzip_json(CONFIG, ADMIN_STORE_PATH, payload)


def admin_emails() -> set[str]:
    try:
        return ADMIN_EMAILS | stored_admin_emails()
    except Exception as exc:  # noqa: BLE001 - fail closed to bootstrap admins only.
        log_event("access_admin_store_unavailable", error=str(exc))
        return set(ADMIN_EMAILS)


def is_admin_email(email: str) -> bool:
    return normalize_email(email) in admin_emails()


def iap_api_url(method: str) -> str:
    if not IAP_RESOURCE:
        raise RuntimeError("MMPRED_IAP_RESOURCE is not configured")
    return f"https://iap.googleapis.com/v1/{IAP_RESOURCE}:{method}"


def iap_request(method: str, payload: dict[str, object]) -> dict[str, object]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        iap_api_url(method),
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {gcs_store.access_token()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"IAP {method} failed: HTTP {exc.code} {text}") from exc


def get_iap_policy() -> dict[str, object]:
    return iap_request("getIamPolicy", {})


def set_iap_policy(policy: dict[str, object]) -> dict[str, object]:
    return iap_request("setIamPolicy", {"policy": policy})


def iap_access_users(policy: dict[str, object]) -> set[str]:
    users: set[str] = set()
    for binding in policy.get("bindings", []):
        if not isinstance(binding, dict) or binding.get("role") != IAP_ACCESS_ROLE:
            continue
        for member in binding.get("members", []):
            member_text = str(member)
            if member_text.startswith("user:"):
                users.add(normalize_email(member_text))
    return users


def access_state(current_email: str) -> dict[str, object]:
    admins = admin_emails()
    payload: dict[str, object] = {
        "current_user": current_email,
        "role": "admin" if current_email in admins else "user",
        "is_admin": current_email in admins,
        "users": [],
    }
    if not payload["is_admin"]:
        return payload
    policy = get_iap_policy()
    users = []
    for email in sorted(iap_access_users(policy) | admins):
        users.append({"email": email, "role": "admin" if email in admins else "user"})
    payload["users"] = users
    payload["iap_resource_configured"] = bool(IAP_RESOURCE)
    return payload


def add_access_user(email: str, role: str, actor: str) -> dict[str, object]:
    normalized = normalize_email(email)
    if role not in {"user", "admin"}:
        raise ValueError("role must be user or admin")
    if not valid_email(normalized):
        raise ValueError("email is invalid")
    with IAP_POLICY_LOCK:
        policy = get_iap_policy()
        bindings = policy.setdefault("bindings", [])
        if not isinstance(bindings, list):
            raise RuntimeError("IAP policy bindings are malformed")
        binding = next((item for item in bindings if isinstance(item, dict) and item.get("role") == IAP_ACCESS_ROLE), None)
        if binding is None:
            binding = {"role": IAP_ACCESS_ROLE, "members": []}
            bindings.append(binding)
        members = binding.setdefault("members", [])
        member = f"user:{normalized}"
        if member not in members:
            members.append(member)
            members.sort()
            set_iap_policy(policy)
        admins = admin_emails()
        if role == "admin" and normalized not in admins:
            admins.add(normalized)
            write_stored_admin_emails(admins)
        log_event("access_user_added", actor=actor, email=normalized, role=role)
    return access_state(actor)


def log_event(event: str, **fields: object) -> None:
    print(
        json.dumps(
            {"event": event, "ts": dt.datetime.now(dt.timezone.utc).isoformat(), **fields},
            sort_keys=True,
        ),
        flush=True,
    )


def invalidate_summary_cache() -> None:
    with SUMMARY_LOCK:
        SUMMARY_CACHE["payloads"] = {}


def db_status() -> dict[str, object]:
    db = engine.connect(CONFIG)
    row_count = db.execute("select count(*) from cohort_rows").fetchone()[0]
    return {
        "sync_in_progress": SYNC_IN_PROGRESS,
        "sync_started_at": SYNC_STARTED_AT,
        "latest_sync": engine.latest_sync(db),
        "row_count": row_count,
    }


def run_sync(callable_, *args: object, **kwargs: object) -> object:
    global SYNC_IN_PROGRESS, SYNC_STARTED_AT
    acquired = SYNC_LOCK.acquire(blocking=False)
    if not acquired:
        return {"status": "busy", "warning": "sync already in progress"}
    SYNC_IN_PROGRESS = True
    SYNC_STARTED_AT = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        result = callable_(*args, **kwargs)
        invalidate_summary_cache()
        return result
    finally:
        SYNC_IN_PROGRESS = False
        SYNC_STARTED_AT = None
        SYNC_LOCK.release()


def warming_payload(db, row_count: int, weekly_count: int) -> dict[str, object]:
    return {
        "status": "warming_up",
        "latest_sync": engine.latest_sync(db),
        "row_count": row_count,
        "subject_row_count": 0,
        "cohort_weeks": weekly_count,
        "predictions": [],
        "horizons": [int(h) for h in CONFIG["prediction"]["horizons"]],
        "summary_by_horizon": {},
    }


def lazy_seed_worker() -> None:
    try:
        result = run_sync(engine.ensure_seeded, CONFIG)
        if isinstance(result, dict) and result.get("status") == "busy":
            log_event("lazy_seed_skipped", reason="locked")
    except Exception as exc:  # noqa: BLE001
        log_event("lazy_seed_failed", error=str(exc))
        traceback.print_exc()


def trigger_lazy_seed() -> None:
    if SYNC_LOCK.locked():
        log_event("lazy_seed_skipped", reason="locked")
        return
    with SEED_TRIGGER_LOCK:
        if SYNC_LOCK.locked():
            log_event("lazy_seed_skipped", reason="locked")
            return
        thread = threading.Thread(target=lazy_seed_worker, name="lazy-seed", daemon=True)
        thread.start()
        log_event("lazy_seed_started")


def maybe_seed(query: dict[str, list[str]]) -> dict[str, object] | None:
    if query.get("seed", ["1"])[0] == "0":
        return None
    db = engine.connect(CONFIG)
    count = db.execute("select count(*) from cohort_rows").fetchone()[0]
    weekly_count = db.execute("select count(*) from cohort_rows where granularity='week'").fetchone()[0]
    if count and weekly_count:
        return None
    trigger_lazy_seed()
    return warming_payload(db, count, weekly_count)


def summary_options(query: dict[str, list[str]]) -> dict[str, object]:
    scope = query.get("scope", ["auto"])[0]
    if scope not in {"auto", "day", "week", "month", "custom"}:
        scope = "auto"
    return {
        "scope": scope,
        "date_from": query.get("date_from", [None])[0],
        "date_to": query.get("date_to", [None])[0],
        "platform": query.get("platform", [None])[0],
        "country": query.get("country", [None])[0],
        "source": query.get("source", query.get("partner", [None]))[0],
        "campaign": query.get("campaign", [None])[0],
    }


def summary_payload(query: dict[str, list[str]]) -> dict[str, object]:
    warming = maybe_seed(query)
    if warming is not None:
        return warming
    options = summary_options(query)
    cache_key = json.dumps(options, sort_keys=True)
    now = time.monotonic()
    with SUMMARY_LOCK:
        payloads = SUMMARY_CACHE.setdefault("payloads", {})
        cached = payloads.get(cache_key) if isinstance(payloads, dict) else None
        if cached and now - float(cached["created_at"]) < SUMMARY_TTL_SECONDS:
            return cached["payload"]  # type: ignore[return-value]
        payload = engine.load_summary_artifact(CONFIG, options) or engine.summary(CONFIG, options)
        if isinstance(payloads, dict):
            payloads[cache_key] = {"payload": payload, "created_at": now}
        return payload


def backtest_payload() -> dict[str, object]:
    return engine.load_backtest_artifact(CONFIG) or engine.backtest(CONFIG)


class Handler(BaseHTTPRequestHandler):
    server_version = "MMPredictions/1.0"

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler naming.
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.respond(*json_bytes({"status": "ok"}), write_body=False)
            return
        if parsed.path == "/":
            self.respond(*html_bytes(), write_body=False)
            return
        if parsed.path.startswith("/static/"):
            self.serve_static(parsed.path.removeprefix("/static/"), write_body=False)
            return
        self.respond(*json_bytes({"error": "not found"}, 404), write_body=False)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming.
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/healthz":
            self.respond(*json_bytes({"status": "ok"}))
            return
        if parsed.path == "/":
            self.respond(*html_bytes())
            return
        if parsed.path.startswith("/static/"):
            self.serve_static(parsed.path.removeprefix("/static/"))
            return
        if parsed.path == "/api/status":
            self.respond(*json_bytes(db_status()))
            return
        if parsed.path == "/api/access":
            try:
                self.respond(*json_bytes(access_state(current_user_email(self.headers))))
            except Exception as exc:  # noqa: BLE001 - surface operational failure.
                self.respond(*json_bytes({"status": "error", "error": str(exc)}, 500))
            return
        if parsed.path == "/api/summary":
            try:
                self.respond(*json_bytes(summary_payload(query)))
            except Exception as exc:  # noqa: BLE001 - surface operational failure.
                self.respond(*json_bytes({"status": "error", "error": str(exc), "trace": traceback.format_exc()}, 500))
            return
        if parsed.path == "/api/backtest":
            try:
                self.respond(*json_bytes(backtest_payload()))
            except Exception as exc:  # noqa: BLE001 - surface operational failure.
                self.respond(*json_bytes({"status": "error", "error": str(exc), "trace": traceback.format_exc()}, 500))
            return
        self.respond(*json_bytes({"error": "not found"}, 404))

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler naming.
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/sync":
            if not self.sync_authorized():
                self.respond(*json_bytes({"error": "unauthorized"}, 401))
                return
            if SYNC_LOCK.locked():
                self.respond(*json_bytes({"status": "busy", "warning": "sync already in progress"}, 409))
                return
            force = query.get("force", ["0"])[0] in {"1", "true", "yes"}
            mode = query.get("mode", ["daily"])[0]
            if mode not in {"all", "weekly", "daily"}:
                self.respond(*json_bytes({"error": "bad_request", "detail": "mode must be one of: all, weekly, daily"}, 400))
                return
            try:
                days = int(query.get("days", ["3"])[0])
                weeks = int(query["weeks"][0]) if "weeks" in query else None
                week_offset = int(query.get("week_offset", ["0"])[0])
            except ValueError:
                self.respond(*json_bytes({"error": "bad_request", "detail": "days, weeks, and week_offset must be integers"}, 400))
                return
            try:
                payload = run_sync(
                    engine.sync_adjust,
                    CONFIG,
                    force=force,
                    mode=mode,
                    days=days,
                    weeks=weeks,
                    week_offset=week_offset,
                )
                if isinstance(payload, dict) and payload.get("status") == "ok":
                    status = 200
                elif isinstance(payload, dict) and payload.get("status") == "busy":
                    status = 409
                else:
                    status = 500
                self.respond(*json_bytes(payload, status))
            except Exception as exc:  # noqa: BLE001
                self.respond(*json_bytes({"status": "error", "error": str(exc), "trace": traceback.format_exc()}, 500))
            return
        if parsed.path == "/api/access/users":
            actor = current_user_email(self.headers)
            if not actor or not is_admin_email(actor):
                self.respond(*json_bytes({"error": "forbidden"}, 403))
                return
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                email = str(body.get("email") or "")
                role = str(body.get("role") or "user")
                self.respond(*json_bytes(add_access_user(email, role, actor)))
            except ValueError as exc:
                self.respond(*json_bytes({"error": "bad_request", "detail": str(exc)}, 400))
            except Exception as exc:  # noqa: BLE001 - surface operational failure.
                self.respond(*json_bytes({"status": "error", "error": str(exc)}, 500))
            return
        self.respond(*json_bytes({"error": "not found"}, 404))

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def serve_static(self, relative: str, write_body: bool = True) -> None:
        if not relative or ".." in relative or relative.startswith("/"):
            self.respond(*json_bytes({"error": "not found"}, 404))
            return
        path = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in path.parents or not path.is_file():
            self.respond(*json_bytes({"error": "not found"}, 404))
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.respond(
            HTTPStatus.OK,
            path.read_bytes(),
            content_type,
            cache_control="public, max-age=300",
            write_body=write_body,
        )

    def respond(
        self,
        status: int,
        body: bytes,
        content_type: str,
        cache_control: str = "no-store",
        write_body: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if content_type.startswith("text/html"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if write_body:
            self.wfile.write(body)

    def authorized(self) -> bool:
        return True

    def sync_authorized(self) -> bool:
        if not SYNC_TOKEN:
            return False
        return token_matches(self.headers.get("X-Sync-Token"), SYNC_TOKEN)


def start_background_seed() -> None:
    try:
        result = run_sync(engine.ensure_seeded, CONFIG)
        if isinstance(result, dict) and result.get("status") == "busy":
            print("Initial sync skipped: sync already in progress")
    except Exception as exc:  # noqa: BLE001
        print(f"Initial sync failed: {exc}")
        traceback.print_exc()


def main() -> int:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if os.environ.get("MMPRED_SYNC_ON_STARTUP", "0") == "1":
        threading.Thread(target=start_background_seed, daemon=True).start()
    print(f"MMPredictions pROAS dashboard listening on :{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
