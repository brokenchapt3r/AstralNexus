"""Independent checks bound to a frozen task specification, not agent output."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlparse, unquote
from typing import Any, Callable


def load_spec(path: Path, task_book: Path) -> dict[str, Any]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise ValueError("acceptance specification must be an object")
    if spec.get("schema") != "astralnexus.acceptance-spec/v1" or spec.get("profile") != "case1":
        raise ValueError("unsupported acceptance specification")
    actual = hashlib.sha256(task_book.read_bytes()).hexdigest()
    if spec.get("task_book_sha256") != actual:
        raise ValueError("acceptance specification does not match the frozen task book")
    spec["spec_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return spec


def case1_api_checks(call: Callable[..., Any], base: str, nonce: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    def record(name: str, ok: bool, detail: Any) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:2000]})
    def request(method: str, path: str, body: Any = None):
        status, text, _ = call(base, method, path, body)
        try:
            return status, json.loads(text)
        except (TypeError, ValueError):
            return status, None
    payload = {"external_id": nonce, "title": nonce, "priority": 1}
    status, created = request("POST", "/api/tickets", payload)
    valid = (status == 201 and isinstance(created, dict)
             and type(created.get("id")) is int and created.get("status") == "open")
    record("case1_create", valid, created)
    if not valid:
        return checks
    ticket = created["id"]
    status, value = request("POST", "/api/tickets", payload)
    record("case1_duplicate", status == 409 and value == {"error": "duplicate_external_id"}, value)
    for name, invalid in (("missing_id", {"title": nonce, "priority": 1}),
                          ("missing_title", {"external_id": nonce, "priority": 1}),
                          ("invalid_priority", {**payload, "priority": 9}),
                          ("non_object", "invalid"), ("malformed_json", b"{")):
        status, value = request("POST", "/api/tickets", invalid)
        record("case1_" + name, status == 400 and value == {"error": "invalid_payload"}, value)
    for path in ("/api/tickets", "/api/tickets?status=open"):
        status, value = request("GET", path)
        rows = value.get("items") if isinstance(value, dict) else None
        valid = (status == 200 and isinstance(rows, list) and type(value.get("count")) is int and value.get("count") == len(rows)
                 and all(isinstance(row, dict) and {"id", "external_id", "title", "priority", "status"} <= row.keys() for row in rows))
        if valid:
            assert isinstance(rows, list)
            ids = [row["id"] for row in rows]
            valid = all(type(i) is int for i in ids) and ids == sorted(ids)
            if "?status=open" in path:
                valid = valid and all(row["status"] == "open" for row in rows)
        record("case1_list_" + ("open" if "?" in path else "all"), valid, value)
    status, value = request("GET", "/api/tickets?status=invalid")
    record("case1_invalid_filter", status == 400 and value == {"error": "invalid_status"}, value)
    status, value = request("PATCH", f"/api/tickets/{ticket}", {"status": "closed"})
    record("case1_close", status == 200 and isinstance(value, dict) and value.get("id") == ticket and value.get("status") == "closed", value)
    for state in ("open", "closed"):
        status, value = request("GET", "/api/tickets?status=" + state)
        rows = value.get("items") if isinstance(value, dict) else None
        valid = status == 200 and isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
        if valid and isinstance(rows, list):
            valid = valid and all(row.get("status") == state for row in rows)
            present = any(row.get("id") == ticket for row in rows)
            valid = valid and present == (state == "closed")
        record("case1_filter_after_close_" + state, valid, value)
    status, value = request("PATCH", f"/api/tickets/{ticket}", {"status": "invalid"})
    record("case1_invalid_update", status == 400 and value == {"error": "invalid_status"}, value)
    status, value = request("PATCH", "/api/tickets/2147483647", {"status": "closed"})
    record("case1_missing_ticket", status == 404 and value == {"error": "not_found"}, value)
    return checks


def case1_browser_checks(base: str, nonce: str) -> list[dict[str, Any]]:
    from playwright.sync_api import sync_playwright
    result = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            lists: list[dict[str, Any]] = []
            posts: list[int] = []
            def observe(response):
                address = urlparse(response.url)
                target = urlparse(base)
                if (address.hostname not in {"127.0.0.1", "localhost"}
                        or address.port != target.port or unquote(address.path) != "/api/tickets"):
                    return
                if response.request.method == "POST":
                    posts.append(response.status)
                elif response.request.method == "GET":
                    try:
                        value = response.json()
                        if isinstance(value, dict) and isinstance(value.get("items"), list):
                            lists.append(value)
                    except ValueError:
                        pass
            page.on("response", observe)
            page.goto(base, wait_until="networkidle")
            count = page.get_by_test_id("ticket-count")
            listing = page.get_by_test_id("ticket-list")
            numbers = set(re.findall(r"\d+", count.inner_text()))
            before = int(next(iter(numbers))) if len(numbers) == 1 else -1
            result.append({"name": "case1_initial_dom", "ok": bool(lists) and before == lists[-1].get("count")
                           and all(str(row.get("title", "")) in listing.inner_text() for row in lists[-1]["items"])
                           and page.get_by_test_id("form").count() == 1, "detail": str(before)})
            for field, value in (("external-id", nonce), ("title", nonce), ("priority", "1")):
                control = page.get_by_test_id("input-" + field)
                if control.evaluate("e => e.tagName") == "SELECT":
                    control.select_option(value)
                else:
                    control.fill(value)
            page.get_by_test_id("submit").click()
            page.wait_for_function("n => (document.querySelector('[data-testid=ticket-count]').textContent.match(/\\d+/g) || []).some(x => Number(x) === n)", arg=before + 1)
            result.append({"name": "case1_dom_create", "ok": nonce in listing.inner_text() and bool(posts) and posts[-1] == 201, "detail": nonce})
            page.get_by_test_id("input-external-id").fill(nonce)
            page.get_by_test_id("input-title").fill(nonce)
            priority = page.get_by_test_id("input-priority")
            if priority.evaluate("e => e.tagName") == "SELECT":
                priority.select_option("1")
            else:
                priority.fill("1")
            page.get_by_test_id("submit").click()
            error = page.get_by_test_id("error")
            error.wait_for(state="visible")
            result.append({"name": "case1_dom_duplicate", "ok": bool(error.inner_text().strip()) and bool(posts) and posts[-1] == 409, "detail": error.inner_text()})
        finally:
            browser.close()
    return result
