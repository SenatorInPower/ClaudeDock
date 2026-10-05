"""Smoke-test the running local Mini App; command delivery is intercepted."""
import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright
import config


def main():
    errors = []
    sent = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 420, "height": 900}, device_scale_factor=1)
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(f'http://127.0.0.1:{config.WEB_PORT}/cu/{config.web_url_secret()}/')
        page.locator('[data-provider="codex"]').click()
        page.wait_for_function("window._sess && window._sess.length && window._sess.every(s=>s.provider==='codex')", timeout=40000)
        page.wait_for_function("document.querySelectorAll('.sess').length > 0")
        cards = page.locator('.sess').count()
        assert cards <= 40, cards
        page.screenshot(path=str(Path('data') / 'codex-miniapp.png'))
        page.locator('.sess').first.click()
        assert page.locator('[onclick="sheetAction(\'compact\')"]').is_hidden()
        assert page.locator('[onclick="sheetAction(\'clear\')"]').is_hidden()
        def intercept(route):
            sent.append(route.request.post_data_json)
            route.fulfill(status=200, content_type='application/json', body=json.dumps(
                {'ok': True, 'queued': True, 'response': 'UI test: delivery intercepted'}))
        page.route('**/api/chat', intercept)
        page.evaluate("canControl=true")
        page.locator('#sh-prompt').fill('UI test only')
        page.evaluate('sendChat()')
        page.wait_for_function("document.getElementById('sh-chat').textContent.includes('delivery intercepted')")
        assert len(sent) == 1 and sent[0]['provider'] == 'codex'
        assert sent[0]['session_id'] == page.evaluate('window._curSid')
        page.evaluate('closeSheet()')
        page.locator('[data-provider="claude"]').click()
        page.wait_for_function("window._sess && window._sess.every(s=>s.provider==='claude')", timeout=40000)
        assert not errors, errors
        print(json.dumps({'ui': 'PASS', 'codex_cards': cards, 'command_provider': sent[0]['provider'],
                          'real_commands_sent': 0, 'page_errors': len(errors)}))
        browser.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # The private app URL must not leak into test output.
        print('UI verification failed: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
