"""Run explicitly configured browser cases and bind their reports to a handoff."""

import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from auto_moniter import candidate_checks, field_handoff
from auto_moniter.directions import Directions
from auto_moniter.redaction import safe
from auto_moniter.storage import write_json

CONFIG = 'browser-cases.json'
ENV = 'AUTO_MONITER_BROWSER_CASES'
CASE_ID = re.compile(r'[a-z][a-z0-9-]{0,63}')


def cases(path, workspace):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if not path.is_file() or path.is_symlink() or info.st_uid != os.getuid() or (info.st_mode & 0o077):
        raise ValueError('浏览器用例配置须为当前用户的私有文件。')
    entries = Directions(path.parent).read(path.name).get('cases', [])
    if not isinstance(entries, list):
        raise ValueError('浏览器用例配置须为列表。')
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('workspace') != str(workspace):
            continue
        case = entry.get('case_id')
        argv = entry.get('argv')
        address = entry.get('url')
        url = urlsplit(address if isinstance(address, str) else '')
        version = entry.get('expected_version')
        if (not isinstance(case, str) or not CASE_ID.fullmatch(case) or case in result
                or not isinstance(argv, list) or not argv
                or any(not isinstance(arg, str) or not arg or '\0' in arg for arg in argv)
                or not Path(argv[0]).is_absolute()
                or url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password
                or url.query or url.fragment or '\\' in address
                or not isinstance(version, str) or not version.strip() or len(version) > 500
                or entry.get('fallback', 'reviewer') not in ('reviewer', 'referee')):
            raise ValueError('浏览器用例配置无效或编号重复。')
        result[case] = {key: entry[key] for key in ('argv', 'url', 'expected_version')}
        result[case]['fallback'] = entry.get('fallback', 'reviewer')
    return result


def reviewer_cases(workspace):
    name = os.environ.get(ENV)
    if not name:
        return ''
    available = cases(Path(name), workspace)
    if not available:
        return ''
    return ('\n现场浏览器交接可在 field_handoffs 的 case_id 标注以下已配置用例。'
            '仅在用例对应缺项时填写，可留空交由人工或现有独立任务。'
            '用例失败时允许 Reviewer 核对已有证据或由裁判安排后续路径。\n'
            + ', '.join(sorted(available)))


def poll(run, value, item, config):
    """One detached execution per case, binding and config; read durable results on restart."""
    request = {'binding': value['binding'], 'revision': value['revision'], 'id': item['id'],
               'case_id': item['case_id'], 'url': config['url'], 'expected_version': config['expected_version']}
    key = candidate_checks.digest([request, config])
    job = run / 'browser-executions' / key
    job.mkdir(parents=True, mode=0o700, exist_ok=True)
    result = Directions(job).read('result.json')
    if result:
        return result
    # Pass the lock descriptor to the detached runner, including across console restart.
    with (job / 'execution.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'running'}
        result = Directions(job).read('result.json')
        if result:
            return result
        launch = Directions(job).read('launch.json')
        attempts = launch.get('attempts', 0)
        if attempts >= 2:
            return {'status': 'failed', 'reason': '浏览器执行进程连续中断，已保留原检查点。'}
        write_json(job / 'launch.json', {'request': request, 'argv': config['argv'], 'attempts': attempts + 1},
                   durable=True, mode=0o600)
        process = subprocess.Popen([sys.executable, '-m', 'auto_moniter.browser_execution', str(job)],
                                   pass_fds=(lock.fileno(),), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        threading.Thread(target=process.wait, daemon=True).start()
    return {'status': 'running'}


def run_case(job):
    from auto_moniter.providers import PreparedCommand
    from auto_moniter.supervisor import run_command

    launch = Directions(job).read('launch.json')
    request = launch['request']
    turn = run_command(PreparedCommand(launch['argv'], stdin_text=json.dumps(request, ensure_ascii=False)),
                       job, timeout_s=240, detect_input_requests=False, max_output_bytes=64 * 1024)
    reason = {3: '测试环境需要验证码输入程序，当前未配置。',
              4: '无头浏览器依赖未安装。',
              5: '浏览器账号配置须保存在当前用户的私有文件中。'}.get(turn.exit_code)
    result = {'status': 'failed', 'reason': reason or f'浏览器用例未完成（退出码 {turn.exit_code}）。'}
    if not (turn.exit_code or turn.timed_out or turn.interruption):
        try:
            output = json.loads(turn.stdout)
            if not isinstance(output, dict):
                raise ValueError()
            report = output.get('receipt', output)
            if (not isinstance(report, dict) or report.get('version') != request['expected_version']
                    or not isinstance(report.get('result'), str) or not report['result'].strip()
                    or len(report['result']) > 4000 or not isinstance(report.get('session_reference'), str)
                    or not report['session_reference'].strip() or len(report['session_reference']) > 500):
                raise ValueError()
            result = {'status':'ready', 'submission': {
                **{key: request[key] for key in ('id', 'binding', 'revision')}, 'receipt': safe(report)}}
            if 'proof' in output:
                result['submission']['proof'] = safe(output['proof'])
        except ValueError:
            result['reason'] = '浏览器回执无效或现场版本与配置不符。'
    write_json(job / 'result.json', result, durable=True, mode=0o600)


if __name__ == '__main__':
    run_case(Path(sys.argv[1]))
