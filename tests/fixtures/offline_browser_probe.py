"""Synthetic acceptance driver for frozen Agent sources; loopback browser only."""
from datetime import datetime, timezone
import json
import importlib
import os
from pathlib import Path
import sys
import tempfile


def main():
    from playwright.sync_api import sync_playwright, expect
    root = Path.cwd()
    sys.path.insert(0, str(root / 'deliverables/app/tests'))
    # This module is materialized from the historical JSON snapshot at runtime.
    LiveServer = importlib.import_module('astral_support').LiveServer
    output = root / 'deliverables/runs/browser'
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    errors = []
    try:
        with tempfile.TemporaryDirectory(prefix='astral-offline-ui-') as scratch:
            with LiveServer(str(Path(scratch) / 'isolated.db'), output / 'server.log') as server:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True,
                        executable_path=os.environ.get('ASTRALNEXUS_TEST_CHROME', 'C:/Program Files/Google/Chrome/Application/chrome.exe'),
                        args=['--disable-background-networking', '--disable-component-update'])
                    try:
                        context = browser.new_context(service_workers='block')
                        context.route('**/*', lambda route: route.continue_()
                                      if route.request.url.startswith(server.base_url + '/') else route.abort())
                        page = context.new_page()
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        response = page.goto(server.base_url + '/')
                        assert response is not None and response.status == 200
                        checks.append({'name': 'frontend_served', 'passed': True})
                        page.locator('#login-username').select_option('emp001')
                        # Login's public role label is rendered before its
                        # async option loads finish and switchView('orders').
                        # Wait for that initial GET rather than clicking Create
                        # while the real UI is still completing login.
                        with page.expect_response(lambda r: '/api/work_orders' in r.url and r.request.method == 'GET') as initial_orders:
                            page.locator('#login-btn').click()
                        assert initial_orders.value.status == 200, initial_orders.value.text()
                        expect(page.locator('#view-orders')).to_be_visible()
                        expect(page.locator('#session-info')).to_contain_text('role=employee')
                        checks.append({'name': 'browser_employee_login', 'passed': True})
                        expect(page.locator('[data-view="assign"]')).to_be_hidden()
                        checks.append({'name': 'employee_assignment_ui_hidden', 'passed': True})
                        page.locator('[data-view="create"]').click()
                        page.wait_for_function("document.querySelector('#create-asset').options.length > 1")
                        page.locator('#create-asset').select_option(index=1)
                        description = 'Offline browser real create and persistence check'
                        page.locator('#create-desc').fill(description)
                        with page.expect_response(lambda r: '/api/work_orders' in r.url and r.request.method == 'POST') as pending:
                            page.locator('#create-form button[type="submit"]').click()
                        created = pending.value
                        assert created.status in (200, 201), created.text()
                        assert created.json()['ok'] is True
                        order_id = created.json()['data']['work_order_id']
                        checks.append({'name': 'browser_creates_real_work_order', 'passed': True})
                        page.locator('[data-view="orders"]').click()
                        expect(page.locator('#orders-body')).to_contain_text(order_id)
                        page.reload()
                        expect(page.locator('#orders-body')).to_contain_text(order_id)
                        page.locator(f'[data-open="{order_id}"]').click()
                        expect(page.locator('#detail-result')).to_contain_text(description)
                        checks.append({'name': 'browser_reload_preserves_order', 'passed': True})
                        page.locator('#logout-btn').click()
                        expect(page.locator('#login-btn')).to_be_visible()
                        page.locator('#login-username').select_option('admin001')
                        page.locator('#login-btn').click()
                        expect(page.locator('#session-info')).to_contain_text('role=admin')
                        expect(page.locator('[data-view="assign"]')).to_be_visible()
                        page.locator('[data-view="stats"]').click()
                        expect(page.locator('#view-stats')).to_be_visible()
                        assert not errors, errors
                        checks.append({'name': 'admin_views_and_no_javascript_errors', 'passed': True})
                        page.screenshot(path=str(output / 'browser.png'), full_page=True)
                    finally:
                        browser.close()
    except Exception as exc:
        checks.append({'name': 'browser_probe_exception', 'passed': False, 'detail': str(exc)})
    failed = sum(not item['passed'] for item in checks)
    summary = {'total': len(checks), 'passed': len(checks) - failed, 'failed': failed}
    (output / 'browser_evidence.json').write_text(json.dumps({
        'generated_at': datetime.now(timezone.utc).isoformat(), 'summary': summary,
        'tests': checks, 'javascript_errors': errors,
        'scope': 'synthetic browser driver against historical source snapshot, not autonomous development'},
        ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
