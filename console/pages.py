"""Render only the requested console page inside the shared navigation shell."""

import html
import re
from pathlib import Path


STATIC = Path(__file__).with_name('static')
PAGES = {
    'overview': ('工作概览', '查看全部运行任务，掌握当前工作与资源概况。'),
    'tasks': ('开发任务', '查看任务列表，进入对应任务工作详情。'),
    'progress': ('实时进展', '选择一个具体任务，持续跟进当前轮次与最新结果。'),
    'resources': ('资源监控', '查看主机和任务的实际资源占用。'),
    'detail': ('任务工作详情', '查看角色工作、执行结论、需求与 Git 追溯记录。'),
    'setup': ('初始化 / Agent 管理', '选择执行器、安装 CLI，并配置模型和认证。'),
}
DETAIL_SECTIONS = {
    'conclusions': ('交互结论', '按轮次查看 worker 执行结论与 reviewer 审查结论。'),
    'git': ('Git 追溯', '查看项目提交、改动与审计记录。'),
    'requirements': ('需求与验收信息', '查看原始需求、完成标准、验收结果与审计问题。'),
    'logs': ('执行日志 · 原始输出', '查看 worker、reviewer 与监督器的原始执行输出。'),
}


def page_route(path: str) -> tuple[str, str, str] | None:
    if path == '/':
        return 'overview', '', ''
    if path.strip('/') in PAGES and path.count('/') == 1 and path != '/detail':
        return path[1:], '', ''
    match = re.fullmatch(r'/tasks/([0-9a-f]{32}|ext-[0-9a-f]{20})(?:/(conclusions|git|requirements|logs))?', path)
    return ('detail', match[1], match[2] or 'home') if match else None


def render_page(page: str, task_id: str, section: str, cwd: Path, *, needs_setup=False) -> bytes:
    shell = (STATIC / 'index.html').read_text()
    content = (STATIC / 'pages' / (page + '.html')).read_text()
    title, subtitle = PAGES[page]
    page_menu = ''
    heading_class = ''
    if page == 'detail':
        section_content = (STATIC / 'pages' / f'detail-{section}.html').read_text()
        if section == 'home':
            icons = {'conclusions': 'terminal', 'git': 'folder', 'requirements': 'task', 'logs': 'server'}
            page_menu = '<nav id="detail-menu" class="detail-menu" aria-label="任务详情菜单">' + ''.join(
                f'<a href="/tasks/{task_id}/{key}" title="{description}"><svg aria-hidden="true"><use href="#i-{icons[key]}"/></svg>'
                f'<span>{label}</span></a>'
                for key, (label, description) in DETAIL_SECTIONS.items()) + '</nav>'
            heading_class = 'detail-heading-with-menu'
            back = '<a class="detail-back" href="/tasks">← 返回开发任务</a>'
        else:
            title, subtitle = DETAIL_SECTIONS[section]
            back = f'<a class="detail-back" href="/tasks/{task_id}">← 返回任务详情</a>'
        content = content.replace('<!-- DETAIL_BACK -->', back).replace('<!-- DETAIL_CONTENT -->', section_content)
    styles = [page]
    if page in {'detail', 'progress'}:
        styles.insert(0, 'work')
    replacements = {'<!-- PAGE_CONTENT -->': content,
                    '<!-- PAGE_MENU -->': page_menu, '__PAGE_HEADING_CLASS__': heading_class,
                    '<!-- PAGE_STYLES -->': '\n'.join(f'<link rel="stylesheet" href="/pages/{name}.css" />' for name in styles),
                    '__PAGE__': page, '__TASK_ID__': task_id, '__DETAIL_SECTION__': section,
                    '__NEEDS_SETUP__': 'true' if needs_setup else 'false',
                    '__WORKSPACE__': str(cwd), '__PAGE_TITLE__': title, '__PAGE_SUBTITLE__': subtitle}
    for key, value in replacements.items():
        shell = shell.replace(key, value if key.startswith('<!--') else html.escape(value, quote=True))
    return shell.encode()


def asset(path: str) -> tuple[Path, str] | None:
    common = {'app.js', 'core.js', 'ui.js', 'task-view.js', 'pagination.js', 'dialogs.js', 'style.css', 'dialogs.css', 'favicon.svg'}
    name = path.removeprefix('/')
    allowed = name in common
    if name.startswith('pages/'):
        base, dot, extension = name[6:].partition('.')
        allowed = base in {*PAGES, 'work'} and extension in {'js', 'css'} and bool(dot)
    if name.startswith('fragments/'):
        allowed = name[10:] in {'create.html', 'direction.html', 'confirm.html', 'git.html', 'resume.html', 'delete.html', 'roles.html'}
    target = STATIC / name
    if not allowed or not target.is_file():
        return None
    mime = {'.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.html': 'text/html'}[target.suffix]
    return target, mime + '; charset=utf-8'
