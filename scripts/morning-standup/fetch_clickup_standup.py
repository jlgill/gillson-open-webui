#!/usr/bin/env python3
"""Fetch fresh ClickUp tasks and build the Daily ClickUp Morning Standup package.

This is the ONLY command the scheduled Open WebUI automation needs to run.
All task data moves between ClickUp, disk, and the build pipeline here, so no
task payload ever passes through the LLM context.

Data source (first available wins, or force with --source):
  rest  CLICKUP_API_TOKEN / CLICKUP_API_TOKEN_FILE  -> ClickUp REST API v2
  mcpo  MCPO_API_KEY / MCPO_API_KEY_FILE            -> mcpo ClickUp MCP proxy
        (MCPO_URL, default http://owui-mcpo:8000/clickup)

Only open tasks are fetched (include_closed=false): the report sections are
built from active tasks only, and closed tickets were 98% of the payload.

Exit codes: 0 success, 1 ClickUp fetch failed (nothing written), 2 build or
verification failed. The last line of output is always STANDUP_RESULT: ...
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

OUTPUT_ROOT = Path("/home/f345ef87/Projects/Morning Standups")
REPORT_TZ = ZoneInfo("America/Los_Angeles")
LISTS = [("901109951078", "IT Projects"), ("182181333", "IT Service Desk")]
BUILD_SCRIPT = "build_clickup_standup.py"
RAW_JSON = "clickup_raw_tasks.json"
NORMALIZED_JSON = "standup_data.json"
ARTIFACTS = [RAW_JSON, NORMALIZED_JSON, "Daily ClickUp Morning Report.html", "run_manifest.json", "README.txt"]

CLICKUP_API = "https://api.clickup.com/api/v2"
DEFAULT_MCPO_URL = "http://owui-mcpo:8000/clickup"
DEFAULT_MCPO_KEY_FILE = "/run/secrets/mcpo_api_key"
MAX_PAGES = 50
HTTP_TIMEOUT = 30
RETRIES = 3


class FetchError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="Report date MM-DD-YYYY (default: today in America/Los_Angeles)")
    parser.add_argument("--output-root", default=str(OUTPUT_ROOT), help=f"Standups root (default: {OUTPUT_ROOT})")
    parser.add_argument("--source", choices=["auto", "rest", "mcpo"], default="auto", help="ClickUp data source")
    return parser.parse_args()


def read_secret(env_name: str, file_env_name: str, default_file: str | None = None) -> str | None:
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    path = os.environ.get(file_env_name) or default_file
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8").strip() or None
    return None


def http_json(url: str, *, method: str = "GET", headers: dict | None = None, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    last_error: Exception = FetchError(f"no attempts made for {url}")
    for attempt in range(1, RETRIES + 1):
        try:
            request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            last_error = FetchError(f"HTTP {error.code} from {url}: {detail}")
            if error.code not in (429, 500, 502, 503, 504):
                raise last_error from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = FetchError(f"{type(error).__name__} from {url}: {error}")
        time.sleep(2 * attempt)
    raise last_error


def _scalar(value, key: str):
    # ClickUp REST nests status/priority as objects; mcpo flattens them to strings.
    if isinstance(value, dict):
        return value.get(key)
    return value


def _int_id(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def normalize_task(task: dict, list_id: str, list_name: str) -> dict:
    return {
        "id": task.get("id"),
        "custom_id": task.get("custom_id"),
        "name": task.get("name"),
        "status": _scalar(task.get("status"), "status"),
        "url": task.get("url"),
        "priority": _scalar(task.get("priority"), "priority"),
        "assignees": [
            {"id": _int_id(person.get("id")), "username": person.get("username")}
            for person in (task.get("assignees") or [])
            if isinstance(person, dict)
        ],
        "tags": [tag.get("name") if isinstance(tag, dict) else tag for tag in (task.get("tags") or [])],
        "due_date": task.get("due_date"),
        "date_closed": task.get("date_closed"),
        "parent": task.get("parent"),
        "list": {"id": list_id, "name": list_name},
    }


def fetch_rest(token: str) -> tuple[list[dict], list[dict]]:
    tasks: list[dict] = []
    stats: list[dict] = []
    for list_id, list_name in LISTS:
        page = 0
        count = 0
        while True:
            url = f"{CLICKUP_API}/list/{list_id}/task?archived=false&include_closed=false&subtasks=true&page={page}"
            payload = http_json(url, headers={"Authorization": token})
            page_tasks = payload.get("tasks") or []
            tasks.extend(normalize_task(task, list_id, list_name) for task in page_tasks)
            count += len(page_tasks)
            if not page_tasks or payload.get("last_page", len(page_tasks) < 100):
                break
            page += 1
            if page >= MAX_PAGES:
                raise FetchError(f"{list_name}: exceeded {MAX_PAGES} pages; refusing to continue")
        stats.append({"id": list_id, "name": list_name, "task_count": count, "pages": page + 1})
    return tasks, stats


def fetch_mcpo(base_url: str, api_key: str) -> tuple[list[dict], list[dict]]:
    tasks: list[dict] = []
    stats: list[dict] = []
    endpoint = f"{base_url.rstrip('/')}/clickup_filter_tasks"
    headers = {"Authorization": f"Bearer {api_key}"}
    for list_id, list_name in LISTS:
        page = 0
        count = 0
        while True:
            body = {"list_ids": [list_id], "include_closed": False, "subtasks": True, "page": page}
            payload = http_json(endpoint, method="POST", headers=headers, body=body)
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, dict) or "tasks" not in payload:
                raise FetchError(f"{list_name}: unexpected mcpo response: {str(payload)[:300]}")
            page_tasks = payload.get("tasks") or []
            tasks.extend(normalize_task(task, list_id, list_name) for task in page_tasks)
            count += len(page_tasks)
            if not payload.get("has_more") or not page_tasks:
                break
            page = int(payload.get("next_page") or page + 1)
            if page >= MAX_PAGES:
                raise FetchError(f"{list_name}: exceeded {MAX_PAGES} pages; refusing to continue")
        stats.append({"id": list_id, "name": list_name, "task_count": count, "pages": page + 1})
    return tasks, stats


def fetch_tasks(source: str) -> tuple[str, list[dict], list[dict]]:
    token = read_secret("CLICKUP_API_TOKEN", "CLICKUP_API_TOKEN_FILE")
    mcpo_key = read_secret("MCPO_API_KEY", "MCPO_API_KEY_FILE", DEFAULT_MCPO_KEY_FILE)
    if source == "auto":
        source = "rest" if token else "mcpo"
    if source == "rest":
        if not token:
            raise FetchError("CLICKUP_API_TOKEN / CLICKUP_API_TOKEN_FILE not set")
        tasks, stats = fetch_rest(token)
        return "clickup_rest_api_v2", tasks, stats
    if not mcpo_key:
        raise FetchError(f"MCPO_API_KEY not set and {DEFAULT_MCPO_KEY_FILE} missing")
    tasks, stats = fetch_mcpo(os.environ.get("MCPO_URL", DEFAULT_MCPO_URL), mcpo_key)
    return "mcpo:clickup_filter_tasks", tasks, stats


def dedupe(tasks: list[dict]) -> tuple[list[dict], int]:
    seen: set[str] = set()
    unique: list[dict] = []
    for task in tasks:
        key = task.get("id")
        if key in seen:
            continue
        seen.add(key)
        unique.append(task)
    return unique, len(tasks) - len(unique)


def fail(reason: str, code: int) -> int:
    print(f"reason: {reason}")
    print("email_delivery: not performed")
    print("STANDUP_RESULT: FAILURE")
    return code


def run(args: argparse.Namespace) -> int:
    report_date = args.date or datetime.now(REPORT_TZ).strftime("%m-%d-%Y")
    datetime.strptime(report_date, "%m-%d-%Y")
    output_root = Path(args.output_root).expanduser().resolve()
    dated_dir = output_root / f"{report_date} Daily Standup"
    print(f"report_date: {report_date}")
    print(f"output_folder: {dated_dir}")

    if not (output_root / BUILD_SCRIPT).is_file():
        return fail(f"{BUILD_SCRIPT} not found in {output_root}", 2)

    try:
        source, tasks, stats = fetch_tasks(args.source)
    except FetchError as error:
        return fail(f"ClickUp fetch failed, nothing written: {error}", 1)

    tasks, duplicates = dedupe(tasks)
    print(f"source: {source} (fresh, include_closed=false, subtasks=true)")
    print("lists:")
    for entry in stats:
        print(f"  {entry['name']} ({entry['id']}): {entry['task_count']} tasks, {entry['pages']} page(s)")
    print(f"tasks_fetched: {len(tasks)}  duplicates_removed: {duplicates}")

    dated_dir.mkdir(parents=True, exist_ok=True)
    raw_path = dated_dir / RAW_JSON
    raw_payload = {
        "export_date": report_date,
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": source,
        "query": {"include_closed": False, "subtasks": True, "archived": False},
        "lists": stats,
        "tasks": tasks,
    }
    raw_path.write_text(json.dumps(raw_payload, indent=2) + "\n", encoding="utf-8")

    build = subprocess.run(
        [sys.executable, BUILD_SCRIPT, "--date", report_date, "--input-raw-json", str(raw_path), "--output-root", str(output_root)],
        cwd=str(output_root),
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        tail = (build.stderr or build.stdout or "").strip()[-1500:]
        return fail(f"build pipeline exited {build.returncode}\n{tail}", 2)

    missing = [name for name in ARTIFACTS if not (dated_dir / name).is_file() or (dated_dir / name).stat().st_size == 0]
    if missing:
        return fail(f"missing or empty artifacts: {', '.join(missing)}", 2)

    report = json.loads((dated_dir / NORMALIZED_JSON).read_text(encoding="utf-8"))
    in_report = ((report.get("diagnostics") or {}).get("task_counts") or {}).get("tasks_after_filtering")
    if in_report != len(tasks):
        return fail(f"reconciliation mismatch: fetched {len(tasks)} tasks but report contains {in_report}", 2)

    metrics = " | ".join(f"{m.get('label')}={m.get('value')}" for m in report.get("hero_metrics") or [])
    print(f"tasks_in_report: {in_report} (reconciled)")
    print(f"hero_metrics: {metrics}")
    print("artifacts:")
    for name in ARTIFACTS:
        print(f"  {name}: {(dated_dir / name).stat().st_size:,} bytes")
    print("email_delivery: not performed")
    print("STANDUP_RESULT: SUCCESS")
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except Exception:  # noqa: BLE001 - the LLM must always see a STANDUP_RESULT line
        return fail("unexpected error\n" + traceback.format_exc()[-1500:], 2)


if __name__ == "__main__":
    sys.exit(main())