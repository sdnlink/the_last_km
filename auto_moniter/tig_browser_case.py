"""Fixed, read-only TIG browser cases for an operator-configured test environment."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from urllib.parse import urlsplit


SUCCESS_PATHS = {
    'member': '/home', 'system': '/system/equipmentConfig',
    'security': '/protectionConfig/knowledgeupdate', 'audit': '/log',
}

class CaptchaAdapterRequired(ValueError):
    pass


def _text_digest(image):
    return hashlib.sha256(image.screenshot()).hexdigest()


def _state(page, origin, version, path):
    page.wait_for_url(origin + path, timeout=15000)
    menus = page.locator('[role=menuitem],.ant-menu-title-content')
    menus.first.wait_for(state='visible', timeout=10000)
    entries = list(dict.fromkeys(x.strip() for x in menus.all_inner_texts() if x.strip()))
    response = page.request.get(origin + '/web.json', max_redirects=0)
    if not response.ok or response.json().get('git.build.version') != version:
        raise ValueError('候选版本与测试环境不一致。')
    return {'path': path, 'menus': entries}


def _captcha_answer(adapter, role, image):
    if not isinstance(adapter, list) or not adapter or not os.path.isabs(adapter[0]):
        raise CaptchaAdapterRequired()
    result = subprocess.run([*adapter, role], input=image, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=35, check=True)
    answer = result.stdout.decode().strip()
    if len(answer) != 4 or not answer.isascii() or not answer.isalnum():
        raise ValueError('验证码输入程序未返回有效结果。')
    return answer


def _login(page, origin, role, account, adapter, *, verify_rejection=False):
    page.goto(origin + '/login')
    user = page.get_by_placeholder('请输入您的用户名')
    password = page.get_by_placeholder('请输入登录密码')
    code = page.get_by_placeholder('请输入验证码')
    captcha = page.locator('.itemImg img')
    submit = page.get_by_role('button', name=re.compile(r'^登\s*录$'))
    user.fill(account['username'])
    password.fill(account['password'])
    captcha.wait_for(state='visible')
    if verify_rejection:
        before = _text_digest(captcha)
        page.wait_for_timeout(1100)
        code.fill('0000')
        with page.expect_response(lambda r: urlsplit(r.url).path == '/api/login/code'):
            submit.click()
        page.get_by_text('验证码错误', exact=True).first.wait_for(state='visible')
        # Explicitly refresh through the page after the observed rejection.
        with page.expect_response(lambda r: urlsplit(r.url).path == '/api/login/code'):
            captcha.click()
        page.wait_for_timeout(300)
        if _text_digest(captcha) == before:
            raise ValueError('刷新后未观察到新的验证码图片。')
        user.fill(account['username'])
        password.fill(account['password'])
    else:
        # Refresh after opening the form so the verification window is known.
        with page.expect_response(lambda r: urlsplit(r.url).path == '/api/login/code'):
            captcha.click()
        page.wait_for_timeout(300)
    code.fill(_captcha_answer(adapter, role, captcha.screenshot()))
    page.wait_for_timeout(1100)
    submitted = datetime.now(timezone.utc).isoformat()
    submit.click()
    return submitted


def _audit_rows(page, username):
    field = page.get_by_placeholder('请输入IP地址/用户名/操作数据 进行查询')
    field.fill(username)
    page.get_by_text('登录', exact=True).first.click()
    page.get_by_text('最近一天', exact=True).click()
    page.get_by_label('操作类型').click()
    page.get_by_text('登录成功', exact=True).click()
    page.get_by_role('button', name='查询').click()
    page.wait_for_timeout(800)
    return [cells[2:4] + cells[5:6] + cells[7:8] for row in page.locator('tbody tr').all()
            if len(cells := row.locator('td').all_inner_texts()) >= 8 and cells[3] == username]


def execute(request, settings):
    from playwright.sync_api import sync_playwright

    case = request['case_id']
    if case not in ('role-login', 'login-audit'):
        raise ValueError('未知 TIG 浏览器用例。')
    url = urlsplit(request['url'])
    origin = f'{url.scheme}://{url.netloc}'
    if origin != settings['origin']:
        raise ValueError('浏览器目标与本地测试配置不一致。')
    version = request['expected_version']
    accounts = settings['accounts']
    adapter = settings.get('captcha_command')
    records = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=settings.get('chrome'), headless=True)
        try:
            roles = SUCCESS_PATHS if case == 'role-login' else ('audit', 'security')
            pages = {}
            before = []
            for role in roles:
                context = browser.new_context(service_workers='block')
                context.route('**/*', lambda route:
                              route.continue_() if route.request.url.startswith((origin + '/', 'data:', 'blob:'))
                              else route.abort())
                page = context.new_page()
                pages[role] = page
                submitted = _login(page, origin, role, accounts[role], adapter,
                                   verify_rejection=case == 'role-login' and role == 'security')
                first = _state(page, origin, version, SUCCESS_PATHS[role])
                page.reload()
                if _state(page, origin, version, SUCCESS_PATHS[role]) != first:
                    raise ValueError('刷新前后的授权菜单或落地页不同。')
                records[role] = {**first, 'login_at': submitted, 'reload_verified': True}
                if case == 'login-audit' and role == 'audit':
                    before = _audit_rows(page, accounts['security']['username'])
            if case == 'login-audit':
                audit = pages['audit']
                audit.reload()
                audit.wait_for_url(origin + '/log')
                after = _audit_rows(audit, accounts['security']['username'])
                delta = [line for line in after if line not in before]
                if len(delta) != 1 or delta[0][0] != '登录成功' or delta[0][-1] != '成功':
                    raise ValueError('未唯一关联本次登录与新增的成功审计行。')
                records['audit_correlation'] = {'before': len(before), 'after': len(after), 'new_row': delta[0],
                                                'device_timezone': settings['device_timezone']}
            else:
                records['security']['captcha_retry_verified'] = True
        finally:
            browser.close()
    reference = 'headless-' + secrets.token_hex(12)
    return {'receipt': {'version': version, 'session_reference': reference,
                        'result': json.dumps(records, ensure_ascii=False, separators=(',', ':'))},
            'proof': {'url': origin + SUCCESS_PATHS['audit'], 'session_reference': reference,
                      'observed_at': datetime.now(timezone.utc).isoformat()}}


def main():
    try:
        request = json.load(sys.stdin)
        path = Path(sys.argv[1])
        info = path.lstat()
        if not path.is_file() or path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return 5
        with path.open(encoding='utf-8') as handle:
            settings = json.load(handle)
        print(json.dumps(execute(request, settings), ensure_ascii=False))
    except CaptchaAdapterRequired:
        return 3
    except ModuleNotFoundError:
        return 4
    except Exception:
        # Browser exceptions can contain entered values. Keep credentials out of logs.
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
