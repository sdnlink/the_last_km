"""Run explicitly configured browser cases and bind their reports to a handoff."""

import fcntl
import hashlib
from importlib.util import find_spec
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from auto_moniter import candidate_checks, field_handoff
from auto_moniter.directions import Directions
from auto_moniter.redaction import safe
from auto_moniter.storage import write_json

CONFIG = 'browser-cases.json'
ENV = 'AUTO_MONITER_BROWSER_CASES'
CASE_ID = re.compile(r'[a-z][a-z0-9-]{0,63}')
TIG_CASES = {'role-login', 'login-audit', 'login-routes'}
CAPTCHA_TIMEOUT = '验证码等待超时，请重新发起浏览器验证。'


def matches(config, binding):
    return (not config.get('task_id') or
            (config['task_id'], config['candidate_tree']) == (binding.get('task_id'), binding.get('tree')))


def matches_item(config, value, item):
    if not matches(config, value.get('binding', {})):
        return False
    if not config.get('handoff_ids'):
        return True
    browser_ids = {row['id'] for row in value.get('items', []) if row['kind'] == 'browser'}
    return (config['handoff_revision'] == value.get('revision') and item.get('id') in config['handoff_ids']
            and set(config['handoff_ids']) <= browser_ids)


def for_handoff(config, value, case_id):
    if not config or not config.get('reuse_on_reopen') or not matches(config, value['binding']):
        return config
    if config.get('handoff_revision') == value['revision']:
        return config
    ids = [row['id'] for row in value['items'] if row['kind'] == 'browser'
           and names_case(config, row, case_id) and not (row.get('claim') or row.get('executor_task'))
           and row.get('url', config['url']) == config['url']
           and row.get('expected_version', config['expected_version']) == config['expected_version']]
    return {**config, 'handoff_revision': value['revision'], 'handoff_ids': ids} if ids else config


def names_case(config, item, case_id):
    assigned = item.get('case_id')
    return assigned == case_id or (
        not assigned and item.get('evidence_needed')
        and candidate_checks.digest({key: item.get(key) for key in field_handoff.REQUEST_FIELDS})
        in config.get('reopen_observation_digests', ()))


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
        task_id, tree = entry.get('task_id'), entry.get('candidate_tree')
        settings = entry.get('tig_settings')
        handoff_ids, revision = entry.get('handoff_ids'), entry.get('handoff_revision')
        scoped = task_id is not None or tree is not None
        if (not isinstance(case, str) or not CASE_ID.fullmatch(case) or case in result
                or url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password
                or url.query or url.fragment or '\\' in address or any(ch.isspace() for ch in address)
                or not isinstance(version, str) or not version.strip() or len(version) > 500
                or scoped and (
                    not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 100
                    or not isinstance(tree, str) or not field_handoff.SHA256.fullmatch(tree))
                or entry.get('fallback', 'reviewer') not in ('reviewer', 'referee')):
            raise ValueError('浏览器用例配置无效或编号重复。')
        config = {'url': address, 'expected_version': version, 'fallback': entry.get('fallback', 'reviewer')}
        if task_id is not None:
            config.update(task_id=task_id, candidate_tree=tree)
        if handoff_ids is not None or revision is not None:
            if (not scoped or not isinstance(handoff_ids, list) or not 1 <= len(handoff_ids) <= 12
                    or any(not isinstance(item, str) or not re.fullmatch(r'(?:[1-9]|1[0-2])', item)
                           for item in handoff_ids)
                    or len(set(handoff_ids)) != len(handoff_ids)
                    or not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{32}', revision)):
                raise ValueError('批量浏览器用例须绑定交接版本和待办编号。')
            config.update(handoff_ids=handoff_ids, handoff_revision=revision)
        reuse = entry.get('reuse_on_reopen', False)
        if type(reuse) is not bool or reuse and not config.get('handoff_ids'):
            raise ValueError('自动续接须登记派单、候选及首批浏览器待办。')
        if reuse:
            config['reuse_on_reopen'] = True
        observations = entry.get('reopen_observation_digests', [])
        if (not isinstance(observations, list) or len(observations) > 12
                or any(not isinstance(value, str) or not field_handoff.SHA256.fullmatch(value) for value in observations)
                or observations and not reuse):
            raise ValueError('自动续接的浏览器观察摘要须绑定已批准的批量用例。')
        if observations:
            config['reopen_observation_digests'] = observations
        if settings is not None:
            if (argv is not None or case not in TIG_CASES or not isinstance(settings, str)
                    or '\0' in settings or not Path(settings).is_absolute()):
                raise ValueError('TIG 用例须指定私有配置的绝对路径。')
            config['argv'] = [sys.executable, '-m', 'auto_moniter.tig_browser_case', settings]
            config.update(_tig_settings_revision(Path(settings)))
            config['runner_digest'] = hashlib.sha256(Path(__file__).with_name('tig_browser_case.py').read_bytes()).hexdigest()
        elif (not isinstance(argv, list) or not argv
              or any(not isinstance(arg, str) or not arg or '\0' in arg for arg in argv)
              or not Path(argv[0]).is_absolute()):
            raise ValueError('浏览器用例须登记可执行文件的绝对路径。')
        else:
            config['argv'] = argv
        keep_session = entry.get('keep_session', False)
        if type(keep_session) is not bool or keep_session and settings is None:
            raise ValueError('持续浏览器会话仅适用于已登记的内置 TIG 用例。')
        if keep_session:
            config['keep_session'] = True
        result[case] = config
    return result


def _tig_settings_revision(path):
    from auto_moniter.tig_browser_case import CaptchaAdapterRequired, captcha_command

    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return {'execution_error': 'TIG 账号和验证码配置须保存在当前用户的私有文件中。'}
        with path.open('rb') as handle:
            content = handle.read(64 * 1024 + 1)
        if len(content) > 64 * 1024:
            return {'execution_error': 'TIG 私有配置文件超过 64 KiB。'}
        settings = json.loads(content)
        if not isinstance(settings, dict):
            raise ValueError()
        command = captcha_command(settings)
        adapter = Path(command[0]).stat() if command else None
        if find_spec('playwright') is None:
            return {'execution_error': '无头浏览器依赖未安装。'}
        result = {'settings_digest': hashlib.sha256(content).hexdigest()}
        if adapter:
            result['adapter_revision'] = [adapter.st_dev, adapter.st_ino, adapter.st_mtime_ns, adapter.st_size]
        if settings.get('interactive_captcha') is True:
            result['interactive_captcha'] = True
        return result
    except CaptchaAdapterRequired:
        return {'execution_error': '测试环境需要获准的验证码输入程序，请核对配置和执行权限。'}
    except FileNotFoundError:
        return {'execution_error': '找不到已登记的 TIG 私有配置文件。'}
    except ValueError:
        return {'execution_error': 'TIG 私有配置须使用 JSON 对象。'}


def reviewer_cases(workspace, task_id, run):
    name = os.environ.get(ENV)
    if not name:
        return ''
    registered = cases(Path(name), workspace)
    binding = {'task_id': task_id}
    if any(config.get('task_id') == task_id for config in registered.values()):
        from auto_moniter.workspace import workspace_fingerprint

        binding['tree'] = workspace_fingerprint(workspace, ignore_dir=run)
    available = [case for case, config in registered.items() if matches(config, binding)
                 and (not config.get('handoff_ids') or config.get('reuse_on_reopen'))]
    if not available:
        return ''
    return ('\n现场浏览器交接可在 field_handoffs 的 case_id 标注以下已配置用例。'
            '仅在用例对应缺项时填写，可留空交由人工或现有独立任务。'
            '用例失败时允许 Reviewer 核对已有证据或由裁判安排后续路径。\n'
            + ', '.join(sorted(available)))


def _job(run, value, item, config):
    item_ids = config.get('handoff_ids') or [item['id']]
    request = {'binding': value['binding'], 'revision': value['revision'], 'id': item_ids[0],
               'case_id': item['case_id'], 'url': config['url'], 'expected_version': config['expected_version']}
    if config.get('handoff_ids'):
        request['item_ids'] = item_ids
    key = candidate_checks.digest([request, config])
    jobs = run / 'browser-executions'
    current = jobs / key
    if current.exists() or not config.get('runner_digest'):
        return current, request
    # Preserve the login started under the same request and approved settings.
    for launch_path in jobs.glob('*/launch.json'):
        launch = Directions(launch_path.parent).read(launch_path.name)
        previous_digest = launch.get('runner_digest')
        if (previous_digest and launch.get('request') == request
                and launch_path.parent.name == candidate_checks.digest(
                    [request, {**config, 'runner_digest': previous_digest}])):
            result = Directions(launch_path.parent).read('result.json')
            if not result or result.get('status') == 'ready':
                return launch_path.parent, request
    return current, request


def _challenge(job):
    challenge = Directions(job).read('captcha.json')
    if (not challenge or challenge['expires'] <= time.time()
            or (job / 'captcha-answer.json').exists()):
        return {}
    return challenge


def submit_captcha(run, value, item, config, nonce, code):
    job, _ = _job(run, value, item, config)
    challenge = _challenge(job)
    if not challenge or challenge['nonce'] != nonce:
        raise ValueError('验证码已过期，请等待下一次请求。')
    otp = challenge.get('kind') == 'otp'
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}' if otp else r'[A-Za-z0-9]{4}', code):
        raise ValueError('请输入六位动态验证码。' if otp else '请输入图片上的四位字母或数字。')
    with (job / 'execution.lock').open('r') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise ValueError('浏览器已退出，请重新启动取证。')
    write_json(job / 'captcha-answer.json', {'nonce': nonce, 'code': code}, durable=True, mode=0o600)


def captcha_retryable(result):
    return (result.get('status') == 'failed'
            and (result.get('code') == 'captcha_timeout' or result.get('reason') == CAPTCHA_TIMEOUT))


def poll(run, value, item, config, *, retry_captcha=False):
    """One detached execution per case, binding and config; read durable results on restart."""
    if config.get('execution_error'):
        return {'status': 'failed', 'reason': config['execution_error']}
    job, request = _job(run, value, item, config)
    job.mkdir(parents=True, mode=0o700, exist_ok=True)
    result = Directions(job).read('result.json')
    if result and not retry_captcha:
        return result
    # Pass the lock descriptor to the detached runner, including across console restart.
    with (job / 'execution.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            challenge = _challenge(job) if config.get('interactive_captcha') else {}
            return {'status': 'needs_input', 'challenge': challenge} if challenge else {'status': 'running'}
        result = Directions(job).read('result.json')
        launch = Directions(job).read('launch.json')
        attempts = launch.get('attempts', 0)
        if retry_captcha:
            if not (config.get('interactive_captcha') and captcha_retryable(result)):
                return result or {'status': 'running'}
            archive = job / f"result-attempt-{launch.get('sequence', attempts)}.json"
            (job / 'result.json').replace(archive)
            attempts = 0
        elif result:
            return result
        if attempts >= 2:
            return {'status': 'failed', 'reason': '浏览器执行进程连续中断，已保留原检查点。'}
        for name in ('captcha.json', 'captcha-answer.json'):
            (job / name).unlink(missing_ok=True)
        write_json(job / 'launch.json', {'request': request, 'argv': config['argv'], 'attempts': attempts + 1,
                                       'interactive_captcha': bool(config.get('interactive_captcha')),
                                       **({'runner_digest': config['runner_digest']} if config.get('runner_digest') else {}),
                                       **({'session_settings_digest': config['settings_digest']} if config.get('keep_session') else {}),
                                       'sequence': launch.get('sequence', launch.get('attempts', 0)) + 1},
                   durable=True, mode=0o600)
        process = subprocess.Popen([sys.executable, '-m', 'auto_moniter.browser_execution', str(job)],
                                   pass_fds=(lock.fileno(),), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        threading.Thread(target=process.wait, daemon=True).start()
    return {'status': 'running'}


def run_case(job):
    from auto_moniter.providers import PreparedCommand, TurnResult
    from auto_moniter.supervisor import run_command

    launch = Directions(job).read('launch.json')
    request = launch['request']
    failed_stage = ''
    if launch.get('session_settings_digest'):
        from auto_moniter import tig_browser_session

        try:
            code, output = tig_browser_session.execute(job.parent.parent, job, request, Path(launch['argv'][-1]),
                                                       launch['session_settings_digest'])
            if code and isinstance(output, dict):
                failed_stage = output.get('failed_stage', '')
            turn = TurnResult('', exit_code=code, stdout=json.dumps(output, ensure_ascii=False))
        except (OSError, ValueError, RuntimeError):
            turn = TurnResult('', exit_code=1)
    else:
        turn = run_command(PreparedCommand(launch['argv'], stdin_text=json.dumps(request, ensure_ascii=False)),
                           job, timeout_s=900 if launch.get('interactive_captcha') else 240,
                           detect_input_requests=False, max_output_bytes=64 * 1024)
    reason = {3: '测试环境需要验证码输入程序，当前未配置。',
              4: '无头浏览器依赖未安装。',
              5: '浏览器账号配置须保存在当前用户的私有文件中。',
              6: 'Chrome 无法验证测试站点的证书，请核对站点和已登记的证书公钥指纹。',
              7: '现场部署版本与当前候选不一致。',
              8: CAPTCHA_TIMEOUT}.get(turn.exit_code)
    if turn.timed_out:
        reason = '无头浏览器用例超时，已回收执行进程。'
    result = {'status': 'failed', 'reason': reason or f'浏览器用例未完成（退出码 {turn.exit_code}）。'}
    if failed_stage:
        result['reason'] += f' 失败阶段：{failed_stage}。'
    if turn.exit_code == 8 and not turn.timed_out and not turn.interruption:
        result['code'] = 'captcha_timeout'
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
            if 'observations' in output:
                if not isinstance(output['observations'], dict) or not output['observations']:
                    raise ValueError()
                observations = safe(output['observations'])
                artifact = job / 'observations.json'
                digest = candidate_checks.digest(observations)
                write_json(artifact, {'request': request, 'session_reference': report['session_reference'],
                                      'observations': observations, 'observations_sha256': digest},
                           durable=True, mode=0o600)
                result['submission']['receipt']['result'] += f' 原始观察见 {artifact}，摘要 {digest}。'
        except ValueError:
            result = {'status': 'failed', 'reason': '浏览器回执无效或现场版本与配置不符。'}
    write_json(job / 'result.json', result, durable=True, mode=0o600)


if __name__ == '__main__':
    run_case(Path(sys.argv[1]))
