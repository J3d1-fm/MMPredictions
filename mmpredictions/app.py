#!/usr/bin/env python3
"""Small Cloud Run web app for MMPredictions pROAS predictions."""

from __future__ import annotations

import datetime as dt
import copy
import email.utils
import hmac
import json
import mimetypes
import os
import pathlib
import re
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
PROJECT_STORE_PATH = "projects/registry.json"
PROJECTS_LOCK = threading.Lock()
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")

SYNC_LOCK = threading.Lock()
SEED_TRIGGER_LOCK = threading.Lock()
SYNC_IN_PROGRESS = False
SYNC_STARTED_AT: str | None = None
SUMMARY_LOCK = threading.Lock()
SUMMARY_CACHE: dict[str, object] = {"payloads": {}}
SUMMARY_TTL_SECONDS = 60.0
BACKTEST_LOCK = threading.Lock()
BACKTEST_CACHE: dict[str, object] = {"payloads": {}}
BACKTEST_TTL_SECONDS = 300.0


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


def project_store_file() -> pathlib.Path:
    configured = os.environ.get("MMPRED_PROJECTS_PATH")
    if configured:
        return pathlib.Path(configured).expanduser()
    base = engine.database_path(CONFIG).parent
    return base / "projects.json"


def default_project() -> dict[str, object]:
    project = CONFIG.get("project", {}) if isinstance(CONFIG.get("project"), dict) else {}
    adjust = CONFIG.get("adjust", {})
    storage = CONFIG.get("storage", {})
    return {
        "id": str(project.get("id") or "default"),
        "name": str(project.get("name") or "Default project"),
        "mmp_provider": str(adjust.get("provider") or "adjust"),
        "mmp_api_token_env": str(adjust.get("api_token_env") or "ADJUST_API_TOKEN"),
        "app_tokens": [str(token) for token in adjust.get("app_tokens", [])],
        "app_token_labels": dict(adjust.get("app_token_labels", {})),
        "google_ads_enabled": False,
        "google_ads_config_path": "",
        "google_ads_customer_ids": [],
        "database_path": str(CONFIG.get("database_path") or ""),
        "gcs_prefix": str(storage.get("gcs_prefix") or "mmpredictions"),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def sanitize_project(raw: dict[str, object], existing_id: str | None = None) -> dict[str, object]:
    project_id = str(raw.get("id") or existing_id or "").strip().lower()
    project_id = re.sub(r"[^a-z0-9_-]+", "-", project_id).strip("-_")
    if not PROJECT_ID_RE.match(project_id):
        raise ValueError("project id must be 2-63 chars: lowercase letters, digits, _ or -")
    name = str(raw.get("name") or project_id).strip()
    if not name:
        raise ValueError("project name is required")
    provider = str(raw.get("mmp_provider") or "adjust").strip().lower()
    if provider not in {"adjust", "custom"}:
        raise ValueError("mmp_provider must be adjust or custom")
    token_env = str(raw.get("mmp_api_token_env") or "ADJUST_API_TOKEN").strip()
    if not re.match(r"^[A-Z_][A-Z0-9_]*$", token_env):
        raise ValueError("MMP token env var must look like ADJUST_API_TOKEN")
    app_tokens_raw = raw.get("app_tokens", [])
    if isinstance(app_tokens_raw, str):
        app_tokens = [item.strip() for item in app_tokens_raw.split(",") if item.strip()]
    else:
        app_tokens = [str(item).strip() for item in app_tokens_raw if str(item).strip()]  # type: ignore[union-attr]
    labels = raw.get("app_token_labels", {})
    if isinstance(labels, str):
        labels = json.loads(labels) if labels.strip() else {}
    if not isinstance(labels, dict):
        raise ValueError("app_token_labels must be an object")
    customer_ids_raw = raw.get("google_ads_customer_ids", [])
    if isinstance(customer_ids_raw, str):
        customer_ids = [item.strip().replace("-", "") for item in customer_ids_raw.split(",") if item.strip()]
    else:
        customer_ids = [str(item).strip().replace("-", "") for item in customer_ids_raw if str(item).strip()]  # type: ignore[union-attr]
    for customer_id in customer_ids:
        if not customer_id.isdigit():
            raise ValueError("Google Ads customer ids must be numeric")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    return {
        "id": project_id,
        "name": name,
        "mmp_provider": provider,
        "mmp_api_token_env": token_env,
        "app_tokens": app_tokens,
        "app_token_labels": {str(k): str(v) for k, v in labels.items()},
        "google_ads_enabled": bool(raw.get("google_ads_enabled")),
        "google_ads_config_path": str(raw.get("google_ads_config_path") or "").strip(),
        "google_ads_customer_ids": customer_ids,
        "database_path": str(raw.get("database_path") or "").strip(),
        "gcs_prefix": str(raw.get("gcs_prefix") or "").strip().strip("/"),
        "created_at": str(raw.get("created_at") or now),
        "updated_at": now,
    }


def read_project_registry() -> dict[str, object]:
    payload = gcs_store.download_gzip_json(CONFIG, PROJECT_STORE_PATH) if gcs_store.enabled(CONFIG) else None
    if payload is None:
        local = project_store_file()
        if local.exists():
            payload = json.loads(local.read_text(encoding="utf-8"))
    projects = payload.get("projects", []) if isinstance(payload, dict) else []
    if not projects:
        projects = [default_project()]
    sanitized = []
    seen = set()
    for raw in projects:
        if not isinstance(raw, dict):
            continue
        project = sanitize_project(raw)
        if project["id"] in seen:
            continue
        seen.add(str(project["id"]))
        sanitized.append(project)
    if not sanitized:
        sanitized = [default_project()]
    active_id = payload.get("active_project_id") if isinstance(payload, dict) else None
    if active_id not in {project["id"] for project in sanitized}:
        active_id = sanitized[0]["id"]
    return {"active_project_id": active_id, "projects": sanitized}


def write_project_registry(registry: dict[str, object]) -> None:
    payload = {
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "active_project_id": registry.get("active_project_id"),
        "projects": registry.get("projects", []),
    }
    if gcs_store.enabled(CONFIG):
        gcs_store.upload_gzip_json(CONFIG, PROJECT_STORE_PATH, payload)
        return
    local = project_store_file()
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def public_project(project: dict[str, object]) -> dict[str, object]:
    return {
        "id": project["id"],
        "name": project["name"],
        "mmp_provider": project["mmp_provider"],
        "mmp_api_token_env": project["mmp_api_token_env"],
        "app_tokens": project.get("app_tokens", []),
        "app_token_count": len(project.get("app_tokens", [])),  # type: ignore[arg-type]
        "app_token_labels": project.get("app_token_labels", {}),
        "google_ads_enabled": bool(project.get("google_ads_enabled")),
        "google_ads_configured": bool(project.get("google_ads_config_path") or project.get("google_ads_customer_ids")),
        "google_ads_config_path": project.get("google_ads_config_path", ""),
        "google_ads_customer_ids": project.get("google_ads_customer_ids", []),
        "created_at": project.get("created_at"),
        "updated_at": project.get("updated_at"),
    }


def project_registry_state() -> dict[str, object]:
    registry = read_project_registry()
    return {
        "active_project_id": registry["active_project_id"],
        "projects": [public_project(project) for project in registry["projects"]],  # type: ignore[index]
    }


def upsert_project(raw: dict[str, object], actor: str) -> dict[str, object]:
    project = sanitize_project(raw)
    with PROJECTS_LOCK:
        registry = read_project_registry()
        projects = [dict(item) for item in registry["projects"]]  # type: ignore[index]
        replaced = False
        for index, existing in enumerate(projects):
            if existing.get("id") == project["id"]:
                project["created_at"] = existing.get("created_at", project["created_at"])
                projects[index] = project
                replaced = True
                break
        if not replaced:
            projects.append(project)
        registry["projects"] = sorted(projects, key=lambda item: str(item.get("name", "")).lower())
        registry["active_project_id"] = project["id"]
        write_project_registry(registry)
    log_event("project_saved", actor=actor, project_id=project["id"])
    return project_registry_state()


def project_by_id(project_id: str | None) -> dict[str, object]:
    registry = read_project_registry()
    requested = project_id or str(registry["active_project_id"])
    for project in registry["projects"]:  # type: ignore[index]
        if project["id"] == requested:
            return project
    raise ValueError(f"unknown project_id: {requested}")


def project_config(project_id: str | None) -> dict[str, object]:
    project = project_by_id(project_id)
    cfg = copy.deepcopy(CONFIG)
    cfg["project"] = {"id": project["id"], "name": project["name"]}
    cfg.setdefault("adjust", {})
    cfg["adjust"]["provider"] = project["mmp_provider"]
    cfg["adjust"]["api_token_env"] = project["mmp_api_token_env"]
    cfg["adjust"]["app_tokens"] = list(project.get("app_tokens", []))  # type: ignore[arg-type]
    cfg["adjust"]["app_token_labels"] = dict(project.get("app_token_labels", {}))  # type: ignore[arg-type]
    cfg.setdefault("google_ads", {})
    cfg["google_ads"].update(
        {
            "enabled": bool(project.get("google_ads_enabled")),
            "config_path": project.get("google_ads_config_path", ""),
            "customer_ids": list(project.get("google_ads_customer_ids", [])),  # type: ignore[arg-type]
        }
    )
    base_db = engine.database_path(CONFIG)
    db_path = str(project.get("database_path") or base_db.with_name(f"{base_db.stem}-{project['id']}{base_db.suffix or '.sqlite3'}"))
    cfg["_database_path_override"] = db_path
    base_prefix = gcs_store.prefix(CONFIG)
    cfg["_gcs_prefix_override"] = str(project.get("gcs_prefix") or f"{base_prefix}/projects/{project['id']}").strip("/")
    return cfg


def query_project_id(query: dict[str, list[str]]) -> str | None:
    return query.get("project_id", query.get("project", [None]))[0]


def invalidate_summary_cache() -> None:
    with SUMMARY_LOCK:
        SUMMARY_CACHE["payloads"] = {}
    with BACKTEST_LOCK:
        BACKTEST_CACHE["payloads"] = {}


def db_status(query: dict[str, list[str]] | None = None) -> dict[str, object]:
    cfg = project_config(query_project_id(query or {}))
    db = engine.connect(cfg)
    row_count = db.execute("select count(*) from cohort_rows").fetchone()[0]
    return {
        "project": cfg.get("project", {}),
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


def warming_payload(config: dict[str, object], db, row_count: int, weekly_count: int) -> dict[str, object]:
    return {
        "status": "warming_up",
        "project": config.get("project", {}),
        "latest_sync": engine.latest_sync(db),
        "row_count": row_count,
        "subject_row_count": 0,
        "cohort_weeks": weekly_count,
        "predictions": [],
        "horizons": [int(h) for h in config["prediction"]["horizons"]],  # type: ignore[index]
        "summary_by_horizon": {},
    }


def lazy_seed_worker(config: dict[str, object]) -> None:
    try:
        result = run_sync(engine.ensure_seeded, config)
        if isinstance(result, dict) and result.get("status") == "busy":
            log_event("lazy_seed_skipped", reason="locked")
    except Exception as exc:  # noqa: BLE001
        log_event("lazy_seed_failed", error=str(exc))
        traceback.print_exc()


def trigger_lazy_seed(config: dict[str, object]) -> None:
    if SYNC_LOCK.locked():
        log_event("lazy_seed_skipped", reason="locked")
        return
    with SEED_TRIGGER_LOCK:
        if SYNC_LOCK.locked():
            log_event("lazy_seed_skipped", reason="locked")
            return
        thread = threading.Thread(target=lazy_seed_worker, args=(config,), name="lazy-seed", daemon=True)
        thread.start()
        log_event("lazy_seed_started")


def maybe_seed(config: dict[str, object], query: dict[str, list[str]]) -> dict[str, object] | None:
    if query.get("seed", ["1"])[0] == "0":
        return None
    db = engine.connect(config)
    count = db.execute("select count(*) from cohort_rows").fetchone()[0]
    weekly_count = db.execute("select count(*) from cohort_rows where granularity='week'").fetchone()[0]
    if count and weekly_count:
        return None
    trigger_lazy_seed(config)
    return warming_payload(config, db, count, weekly_count)


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
    cfg = project_config(query_project_id(query))
    warming = maybe_seed(cfg, query)
    if warming is not None:
        return warming
    options = summary_options(query)
    cache_key = json.dumps({"project_id": cfg.get("project", {}).get("id"), **options}, sort_keys=True)
    now = time.monotonic()
    with SUMMARY_LOCK:
        payloads = SUMMARY_CACHE.setdefault("payloads", {})
        cached = payloads.get(cache_key) if isinstance(payloads, dict) else None
        if cached and now - float(cached["created_at"]) < SUMMARY_TTL_SECONDS:
            return cached["payload"]  # type: ignore[return-value]
        payload = engine.load_summary_artifact(cfg, options) or engine.summary(cfg, options)
        if isinstance(payloads, dict):
            payloads[cache_key] = {"payload": payload, "created_at": now}
        return payload


def query_int(query: dict[str, list[str]], key: str, default: int, minimum: int = 0, maximum: int = 5000) -> int:
    try:
        value = int(query.get(key, [str(default)])[0])
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def backtest_payload() -> dict[str, object]:
    return backtest_payload_for_project({}, None)


def backtest_payload_for_project(query: dict[str, list[str]], project_id: str | None) -> dict[str, object]:
    cfg = project_config(project_id)
    full_details = query.get("details", ["0"])[0].lower() in {"1", "true", "yes", "full"}
    cache_key = json.dumps(
        {
            "project_id": cfg.get("project", {}).get("id"),
            "details": full_details,
            "row_limit": query_int(query, "row_limit", 500),
            "retention_row_limit": query_int(query, "retention_row_limit", 500),
        },
        sort_keys=True,
    )
    now = time.monotonic()
    with BACKTEST_LOCK:
        payloads = BACKTEST_CACHE.setdefault("payloads", {})
        cached = payloads.get(cache_key) if isinstance(payloads, dict) else None
        if cached and now - float(cached["created_at"]) < BACKTEST_TTL_SECONDS:
            return cached["payload"]  # type: ignore[return-value]
    payload = engine.load_backtest_artifact(cfg) or engine.backtest(cfg)
    if full_details:
        result = payload
    else:
        result = engine.compact_backtest_payload(
            payload,
            row_limit=query_int(query, "row_limit", 500),
            retention_row_limit=query_int(query, "retention_row_limit", 500),
        )
    with BACKTEST_LOCK:
        payloads = BACKTEST_CACHE.setdefault("payloads", {})
        if isinstance(payloads, dict):
            payloads[cache_key] = {"payload": result, "created_at": now}
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "MMPredictions/1.1"

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
            self.respond(*json_bytes(db_status(query)))
            return
        if parsed.path == "/api/projects":
            self.respond(*json_bytes(project_registry_state()))
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
                self.respond(*json_bytes(backtest_payload_for_project(query, query_project_id(query))))
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
                cfg = project_config(query_project_id(query))
                payload = run_sync(
                    engine.sync_adjust,
                    cfg,
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
        if parsed.path == "/api/projects":
            actor = current_user_email(self.headers)
            if not actor or not is_admin_email(actor):
                self.respond(*json_bytes({"error": "forbidden"}, 403))
                return
            try:
                body = self.read_json_body()
                self.respond(*json_bytes(upsert_project(body, actor)))
            except ValueError as exc:
                self.respond(*json_bytes({"error": "bad_request", "detail": str(exc)}, 400))
            except Exception as exc:  # noqa: BLE001
                self.respond(*json_bytes({"status": "error", "error": str(exc), "trace": traceback.format_exc()}, 500))
            return
        if parsed.path == "/api/projects/sync":
            actor = current_user_email(self.headers)
            if not actor or not is_admin_email(actor):
                self.respond(*json_bytes({"error": "forbidden"}, 403))
                return
            if SYNC_LOCK.locked():
                self.respond(*json_bytes({"status": "busy", "warning": "sync already in progress"}, 409))
                return
            try:
                body = self.read_json_body()
                cfg = project_config(str(body.get("project_id") or ""))
                mode = str(body.get("mode") or "daily")
                if mode not in {"daily", "weekly", "all"}:
                    raise ValueError("mode must be one of: daily, weekly, all")
                payload = run_sync(
                    engine.sync_adjust,
                    cfg,
                    force=bool(body.get("force")),
                    mode=mode,
                    days=int(body.get("days") or 3),
                    weeks=int(body["weeks"]) if body.get("weeks") not in {None, ""} else None,
                    week_offset=int(body.get("week_offset") or 0),
                )
                status = 200 if isinstance(payload, dict) and payload.get("status") == "ok" else 409 if isinstance(payload, dict) and payload.get("status") == "busy" else 500
                self.respond(*json_bytes(payload, status))
            except ValueError as exc:
                self.respond(*json_bytes({"error": "bad_request", "detail": str(exc)}, 400))
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

    def read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body

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
        result = run_sync(engine.ensure_seeded, project_config(None))
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
