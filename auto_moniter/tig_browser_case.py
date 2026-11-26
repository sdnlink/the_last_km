"""Fixed, read-only TIG browser cases for an operator-configured test environment."""

from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from urllib.parse import urlsplit


SUCCESS_PATHS = {
    'member': '/home', 'system': '/system/equipmentConfig',
    'security': '/protectionConfig/knowledgeupdate', 'audit': '/log',
}

HISTORY_PATH = '/blockhostlist/history'
WHITELIST_PATH = '/protectionConfig/globalWhitelist'
WHITELIST_ROUTES = {
    '/knowledgeBase/whitelist': ('知识库白名单（IoC）', '域名情报白名单', ('当前功能',)),
    '/protectionConfig/detectionStrategy?tab=1': (
        '内部终端IP地址白名单', '威胁情报', ('监测 IP 段设置', '内部终端IP地址白名单')),
}

class CaptchaAdapterRequired(ValueError):
    pass


class CaptchaExpired(ValueError):
    pass


class WrongDeployment(ValueError):
    pass


def human_captcha(role, image=None):
    from auto_moniter.directions import Directions
    from auto_moniter.storage import write_json

    directory = Path.cwd()
    nonce = secrets.token_hex(16)
    expires = time.time() + 180
    request = directory / 'captcha.json'
    response = directory / 'captcha-answer.json'
    challenge = {'nonce': nonce, 'role': role, 'expires': expires,
                 'kind': 'image' if image is not None else 'otp'}
    if image is not None:
        challenge['image'] = base64.b64encode(image).decode('ascii')
    pattern = r'[A-Za-z0-9]{4}' if image is not None else r'[0-9]{6}'
    write_json(request, challenge, durable=True, mode=0o600)
    try:
        while time.time() < expires:
            answer = Directions(directory).read(response.name)
            if answer.get('nonce') == nonce and re.fullmatch(pattern, answer.get('code', '')):
                return answer['code']
            time.sleep(.25)
        raise CaptchaExpired()
    finally:
        response.unlink(missing_ok=True)
        request.unlink(missing_ok=True)


def captcha_command(settings):
    adapter = settings.get('captcha_command')
    if adapter is None and settings.get('interactive_captcha') is True:
        return None
    if (not isinstance(adapter, list) or not adapter
            or any(not isinstance(arg, str) or not arg or '\0' in arg for arg in adapter)
            or not os.path.isabs(adapter[0]) or not os.path.isfile(adapter[0])
            or not os.access(adapter[0], os.X_OK)):
        raise CaptchaAdapterRequired()
    return adapter


def _text_digest(image):
    return hashlib.sha256(image.screenshot()).hexdigest()


def _state(page, origin, version, path):
    page.wait_for_url(origin + path, timeout=15000)
    menus = page.locator('[role=menuitem],.ant-menu-title-content')
    menus.first.wait_for(state='visible', timeout=10000)
    entries = list(dict.fromkeys(x.strip() for x in menus.all_inner_texts() if x.strip()))
    observed_version = page.evaluate("""async () => {
        const response = await fetch('/web.json', {redirect: 'error', cache: 'no-store'});
        return response.ok ? (await response.json())['git.build.version'] : null;
    }""")
    if observed_version != version:
        raise ValueError('候选版本与测试环境不一致。')
    return {'path': path, 'menus': entries}


def _captcha_answer(adapter, role, image):
    if adapter is None:
        return human_captcha(role, image)
    result = subprocess.run([*adapter, role], input=image, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=35, check=True)
    answer = result.stdout.decode().strip()
    if len(answer) != 4 or not answer.isascii() or not answer.isalnum():
        raise ValueError('验证码输入程序未返回有效结果。')
    return answer


def _login(page, origin, role, account, adapter, *, verify_rejection=False, interactive=False, retry=True):
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
    if interactive:
        with page.expect_response(lambda r: urlsplit(r.url).path == '/login'
                                  and r.request.method == 'POST', timeout=15000) as response:
            submit.click()
        result = response.value.json()
        if (retry and isinstance(result, dict) and result.get('success') is False
                and '验证码' in str(result.get('msg', ''))):
            return _login(page, origin, role, account, adapter, interactive=True, retry=False)
        otp = page.get_by_placeholder('请输入6位验证码')
        page.wait_for_function("""() => location.pathname !== '/login'
            || [...document.querySelectorAll('input[autocomplete="one-time-code"]')].some(input =>
                input.getClientRects().length > 0)""", timeout=15000)
        if otp.is_visible():
            otp.fill(human_captcha(role))
            otp.press('Enter')
    else:
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


def _route_url(page, origin):
    url = urlsplit(page.url)
    if f'{url.scheme}://{url.netloc}' != origin:
        raise ValueError('浏览器离开了登记的站点。')
    # Redirects may include sensitive query parameters. Retain only the known tab selectors.
    return url.path + ('?' + url.query if url.query in ('tab=1', 'tab=activeDetection') else '')


def _history_snapshot(page, origin, action):
    with page.expect_response(lambda response:
                              urlsplit(response.url).path == '/api/block_host/list'
                              and response.request.method == 'POST', timeout=15000) as event:
        action()
    response = event.value
    page.wait_for_url(origin + HISTORY_PATH, timeout=10000)
    table = page.locator('.ant-table').first
    table.wait_for(state='visible')
    data = response.request.post_data_json
    result = response.json()
    return {'url': _route_url(page, origin), 'at': datetime.now(timezone.utc).isoformat(),
            'status_filter': data.get('status'),
            'http_status': response.status, 'api_status': result.get('status'),
            'success': result.get('success'), 'result_count': len(result.get('results') or []),
            'table_visible': table.is_visible()}


def _whitelist_snapshot(page, origin, path, tab, panels):
    from playwright.sync_api import TimeoutError

    page.wait_for_url(origin + path, timeout=10000)
    selected = page.get_by_role('tab', name=tab, exact=True)
    selected.wait_for(state='visible')
    try:
        selected.and_(page.locator('[aria-selected="true"]')).wait_for(timeout=4000)
    except TimeoutError:
        pass
    content = page.locator('[role="tabpanel"]:visible').first
    content.wait_for(state='visible')
    visible_panels = []
    for label in panels:
        normalized = re.escape(label).replace(r'\ ', r'\s*')
        panel = content.get_by_text(re.compile(normalized)).and_(page.locator(':visible')).first
        if panel.is_visible():
            visible_panels.append(label)
    active = page.locator('[role="tab"][aria-selected="true"]:visible').first
    return {'url': _route_url(page, origin), 'at': datetime.now(timezone.utc).isoformat(), 'tab': tab,
            'active_tab': active.inner_text().strip() if active.is_visible() else '',
            'panels': visible_panels, 'missing_panels': [label for label in panels if label not in visible_panels]}


def _session_identity(page, account, others):
    # Return comparisons only. Never export stored values or tokens.
    return page.evaluate("""([account, others]) => {
        let app;
        try {
            app = JSON.parse(JSON.parse(sessionStorage.getItem('persist:root') || '{}').app || '{}');
        } catch {
            return {storage_readable: false};
        }
        const text = (document.body?.innerText || '').normalize();
        return {
            storage_readable: true,
            own_account_in_session: app.user_name === account,
            other_test_account_in_session: others.includes(app.user_name),
            other_test_account_visible: others.some(name => text.includes(name))
        };
    }""", [account, others])


def _direct_snapshot(page, origin, path, marker, role, identity):
    def observe():
        page.wait_for_function(
            """([target, selector, label, landing]) => {
                const url = location.pathname + location.search;
                const visible = el => el.getClientRects().length > 0;
                return [...document.querySelectorAll('img[src="/img/404.png"]')].some(visible)
                    || (url !== target && [landing, '/login', '/403'].includes(location.pathname))
                    || [...document.querySelectorAll(selector)].some(el =>
                        visible(el) && (!label || el.textContent.trim() === label));
            }""",
            arg=[path, marker[0], marker[1], SUCCESS_PATHS[role]], timeout=10000)
        url = _route_url(page, origin)
        denied = page.locator('img[src="/img/404.png"]').first.is_visible() or url == '/403'
        target_visible = (page.get_by_role('tab', name=marker[1], exact=True).first.is_visible()
                          if marker[1] else page.locator(marker[0]).first.is_visible())
        return {'url': url, 'target_panel_visible': target_visible, 'not_found_visible': denied,
                'session_identity': identity(),
                'result': 'denied' if denied else 'visible' if url == path and target_visible
                else 'redirect' if url != path else 'unconfirmed'}

    page.goto(origin + path)
    direct = observe()
    page.reload()
    return {'direct': direct, 'refresh': observe()}


def _route_checks(pages, origin, version, records, accounts):
    from playwright.sync_api import TimeoutError

    member = pages['member']
    member.get_by_text('内网拦截资产', exact=True).first.click()
    history = member.get_by_role('button', name='封禁历史列表', exact=True)
    history.wait_for(state='visible')
    history_source = _route_url(member, origin)
    navigation = {HISTORY_PATH: {'role': 'member',
                                 'source': history_source,
                                 'click': _history_snapshot(member, origin, history.click),
                                 'refresh': _history_snapshot(member, origin, member.reload)}}
    navigation[HISTORY_PATH]['return'] = _observe_browser_back(member, origin, history_source)
    security = pages['security']
    desktop = {}
    knowledge = '/knowledgeBase/whitelist'
    for width in (1440, 1024):
        security.set_viewport_size({'width': width, 'height': 900})
        paths = desktop[str(width)] = {}
        for path, (row, tab, panels) in WHITELIST_ROUTES.items():
            observed = paths[path] = {}
            security.goto(origin + WHITELIST_PATH)
            try:
                source = security.get_by_role('row').filter(has_text=row)
                source.get_by_role('button', name='新增监测白名单').click(timeout=10000)
                observed['click'] = _whitelist_snapshot(security, origin, path, tab, panels)
                security.reload()
                observed['refresh'] = _whitelist_snapshot(security, origin, path, tab, panels)
                if path == knowledge:
                    security.get_by_role('button', name=re.compile(r'跳\s*转')).first.click(timeout=10000)
                    security.wait_for_url(origin + WHITELIST_PATH, timeout=10000)
                else:
                    observed['return'] = _observe_browser_back(security, origin, WHITELIST_PATH)
            except TimeoutError:
                observed['incomplete'] = '页面或控件在规定时间内未出现，参见已经记录的步骤。'
            observed['last_url'] = _route_url(security, origin)
            observed['vertical_sidebar_visible'] = security.locator('.ant-layout-sider').first.is_visible()
    security.set_viewport_size({'width': 1440, 'height': 900})
    for path, (row, tab, panels) in WHITELIST_ROUTES.items():
        # Use the real row button for each transition; opening the target does not test the button.
        security.goto(origin + WHITELIST_PATH)
        source = security.get_by_role('row').filter(has_text=row)
        source.get_by_role('button', name='新增监测白名单').click()
        click = _whitelist_snapshot(security, origin, path, tab, panels)
        security.reload()
        navigation[path] = {'role': 'security', 'click': click,
                            'refresh': _whitelist_snapshot(security, origin, path, tab, panels),
                            'return': _observe_browser_back(security, origin, WHITELIST_PATH)}
    active_path = '/protectionConfig/detectionStrategy?tab=activeDetection'
    security.goto(origin + active_path)
    active_probe = _active_detection_snapshot(security, origin)
    security.reload()
    active_detection = {'direct': active_probe, 'refresh': _active_detection_snapshot(security, origin)}
    markers = {HISTORY_PATH: ('.ant-table', ''),
               **{path: ('[role="tab"]', values[1]) for path, values in WHITELIST_ROUTES.items()}}
    direct = {}
    sessions = {}
    source_matrix = {}
    for role, page in pages.items():
        page.goto(origin + SUCCESS_PATHS[role])
        observed = _state(page, origin, version, SUCCESS_PATHS[role])
        labels = page.get_by_text(re.compile(r'^【[^】]+】$')).all_inner_texts()
        sessions[role] = {**observed, 'login_at': records[role]['login_at'],
                          'displayed_role_labels': list(dict.fromkeys(labels)),
                          'layout': 'vertical' if page.locator('.ant-layout-sider').first.is_visible()
                          else 'horizontal'}
        account = accounts[role]['username']
        others = [row['username'] for key, row in accounts.items() if key != role]
        identity = lambda: _session_identity(page, account, others)
        direct[role] = [_direct_snapshot(page, origin, path, marker, role, identity)
                        for path, marker in markers.items()]
        source_matrix[role] = _source_navigation(page, origin, history_source, markers, direct[role])
    return {'navigation': navigation, 'active_detection': active_detection,
            'direct_route_order': list(markers), 'direct': direct,
            'sessions': sessions, 'desktop_navigation': desktop, 'source_navigation': source_matrix,
            'contexts': len({id(page.context) for page in pages.values()}),
            'layout_scope': '仅记录站点本次实际提供的布局，未更改设备或用户配置。'}


def _source_navigation(page, origin, history_source, markers, targets):
    from playwright.sync_api import TimeoutError

    observed = {}
    for path, target in zip(markers, targets, strict=True):
        source = history_source if path == HISTORY_PATH else WHITELIST_PATH
        page.goto(origin + source)
        if path == HISTORY_PATH:
            button = page.get_by_role('button', name='封禁历史列表', exact=True).first
        else:
            row, _, _ = WHITELIST_ROUTES[path]
            button = page.get_by_role('row').filter(has_text=row).get_by_role(
                'button', name='新增监测白名单').first
        try:
            button.wait_for(state='visible', timeout=3000)
        except TimeoutError:
            pass
        entry = observed[path] = {
            'source': source, 'observed_source': _route_url(page, origin),
            'source_not_found_visible': page.locator('img[src="/img/404.png"]').first.is_visible(),
            'button_visible': button.is_visible(), 'target': target['direct']['result'],
        }
        if entry['observed_source'] == source and entry['button_visible'] and entry['target'] in ('denied', 'redirect'):
            button.click(timeout=5000)
            try:
                page.wait_for_url(lambda url: url != origin + source, timeout=5000)
            except TimeoutError:
                pass
            entry['click_url'] = _route_url(page, origin)
            entry['click_not_found_visible'] = page.locator('img[src="/img/404.png"]').first.is_visible()
            page.reload()
            entry['refresh_url'] = _route_url(page, origin)
            entry['refresh_not_found_visible'] = page.locator('img[src="/img/404.png"]').first.is_visible()
    return observed


def _observe_browser_back(page, origin, expected):
    from playwright.sync_api import TimeoutError

    try:
        page.go_back(timeout=10000)
        page.wait_for_url(origin + expected, timeout=10000)
    except TimeoutError:
        return {'expected_url': expected, 'observed_url': _route_url(page, origin), 'matched': False}
    return {'expected_url': expected, 'observed_url': _route_url(page, origin), 'matched': True}


def _active_detection_snapshot(page, origin):
    probe = page.get_by_role('tab', name='主动探测配置', exact=True)
    selected = page.locator('[role="tab"][aria-selected="true"]:visible').first
    selected.wait_for(state='visible')
    return {'url': _route_url(page, origin), 'tab_available': probe.is_visible(),
            'active_tab': selected.inner_text().strip()}


def chrome_args(settings):
    pin = settings.get('tls_public_key_sha256')
    browser_args = []
    if pin is not None:
        if not isinstance(pin, str) or not re.fullmatch(r'[A-Za-z0-9+/]{43}=', pin):
            raise ValueError('部署证书公钥指纹无效。')
        browser_args.append('--ignore-certificate-errors-spki-list=' + pin)
    return browser_args


class BrowserSession:
    def __init__(self, browser):
        self.browser = browser
        self.pages = {}
        self.stage = '准备验证'


def execute(request, settings, *, session=None):
    from playwright.sync_api import sync_playwright

    case = request['case_id']
    if case not in ('role-login', 'login-audit', 'login-routes'):
        raise ValueError('未知 TIG 浏览器用例。')
    url = urlsplit(request['url'])
    origin = f'{url.scheme}://{url.netloc}'
    if origin != settings['origin']:
        raise ValueError('浏览器目标与本地测试配置不一致。')
    version = request['expected_version']
    accounts = settings['accounts']
    adapter = captcha_command(settings)
    if session is None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=settings.get('chrome'),
                                                 headless=settings.get('visible_browser') is not True,
                                                 args=chrome_args(settings))
            try:
                return execute(request, settings, session=BrowserSession(browser))
            finally:
                browser.close()
    browser = session.browser
    pages = session.pages
    records = {}
    roles = ('audit', 'security') if case == 'login-audit' else SUCCESS_PATHS
    before = []
    for role in roles:
        session.stage = f'核对 {role} 登录会话'
        saved = pages.get(role)
        if saved and not (case in ('role-login', 'login-audit') and role == 'security'):
            page, record = saved
            page.goto(origin + SUCCESS_PATHS[role])
            if urlsplit(page.url).path != '/login':
                observed = _state(page, origin, version, SUCCESS_PATHS[role])
                records[role] = {**record, **observed}
        if role not in records:
            if saved:
                saved[0].context.close()
                pages.pop(role)
            context = browser.new_context(service_workers='block')
            try:
                context.route('**/*', lambda route:
                              route.continue_() if route.request.url.startswith((origin + '/', 'data:', 'blob:'))
                              else route.abort())
                page = context.new_page()
                session.stage = f'核对 {role} 测试入口版本'
                response = page.goto(origin + '/web.json', timeout=15000)
                if not response or not response.ok or response.json().get('git.build.version') != version:
                    raise WrongDeployment()
                session.stage = f'登录 {role}'
                submitted = _login(page, origin, role, accounts[role], adapter,
                                   verify_rejection=case == 'role-login' and role == 'security',
                                   interactive=settings.get('interactive_captcha') is True)
                session.stage = f'核对 {role} 登录落点及菜单'
                first = _state(page, origin, version, SUCCESS_PATHS[role])
                page.reload()
                if _state(page, origin, version, SUCCESS_PATHS[role]) != first:
                    raise ValueError('刷新前后的授权菜单或落地页不同。')
            except Exception:
                context.close()
                raise
            records[role] = {**first, 'login_at': submitted, 'reload_verified': True}
            pages[role] = page, records[role]
        if case == 'login-audit' and role == 'audit':
            before = _audit_rows(page, accounts['security']['username'])
    observed_pages = {role: pages[role][0] for role in roles}
    session.stage = '检查登录后页面'
    if case == 'login-audit':
        audit = observed_pages['audit']
        audit.reload()
        audit.wait_for_url(origin + '/log')
        after = _audit_rows(audit, accounts['security']['username'])
        delta = [line for line in after if line not in before]
        if len(delta) != 1 or delta[0][0] != '登录成功' or delta[0][-1] != '成功':
            raise ValueError('未唯一关联本次登录与新增的成功审计行。')
        records['audit_correlation'] = {'before': len(before), 'after': len(after), 'new_row': delta[0],
                                        'device_timezone': settings['device_timezone']}
    elif case == 'login-routes':
        records = _route_checks(observed_pages, origin, version, records, accounts)
    else:
        records['security']['captcha_retry_verified'] = True
    reference = ('visible-' if settings.get('visible_browser') is True else 'headless-') + secrets.token_hex(12)
    if case == 'login-routes':
        return {'receipt': {'version': version, 'session_reference': reference,
                            'result': '已记录实际导航、刷新、返回、桌面宽度、四角色菜单和独立会话的页面访问结果。'},
                'proof': {'url': origin + SUCCESS_PATHS['audit'], 'session_reference': reference,
                          'observed_at': datetime.now(timezone.utc).isoformat()},
                'observations': records}
    return {'receipt': {'version': version, 'session_reference': reference,
                        'result': json.dumps(records, ensure_ascii=False, separators=(',', ':'))},
            'proof': {'url': origin + SUCCESS_PATHS['audit'], 'session_reference': reference,
                      'observed_at': datetime.now(timezone.utc).isoformat()}}


def run_request(request, settings, *, session=None):
    try:
        return 0, execute(request, settings, session=session)
    except CaptchaAdapterRequired:
        return 3, None
    except CaptchaExpired:
        return 8, None
    except ModuleNotFoundError:
        return 4, None
    except WrongDeployment:
        return 7, None
    except Exception as exc:
        code = 6 if 'net::ERR_CERT_' in str(exc) else 1
        return code, {'failed_stage': session.stage} if session is not None else None


def main():
    try:
        request = json.load(sys.stdin)
        path = Path(sys.argv[1])
        info = path.lstat()
        if not path.is_file() or path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return 5
        with path.open(encoding='utf-8') as handle:
            settings = json.load(handle)
    except (OSError, ValueError):
        return 5
    code, output = run_request(request, settings)
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
