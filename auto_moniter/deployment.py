"""Deployment execution guidance and evidence required for final delivery."""

import json
import hashlib
import re
from pathlib import Path

from auto_moniter.requirements_review import requirement_hash

STAGES = ('preflight', 'build', 'package', 'deploy', 'verify')


def evidence_path(cwd, run_dir):
    return Path(cwd) / 'deployment-evidence' / Path(run_dir).name / 'result.json'


def instructions(role, config, run_dir):
    common = (
        '\nDeployment task: the worker executes the authorized operational request, without requiring source edits, '
        'function assignments, failing unit tests or preapproved remote check IDs. '
        'Use these stages: preflight -> build -> package -> deploy -> verify. '
        'Reuse existing build/deployment entry points and the provided hosts, directories and authentication. '
        'Inspect remote environments before changing them; resolve routine choices from the existing installation. '
        'Missing information that can be discovered by authorized inspection is not a reason to stop requirement review. '
        'Do not ask again for deployment authorization or credentials already supplied. '
        'Do not invent credentials or expand the requested scope. Protect existing data and management access. '
        'Use supplied secrets privately for authentication; never put passwords or tokens in evidence, source, '
        'shell command arguments, Git commits or displayed logs. '
        'Local tests and git diff --check are supplemental; they cannot prove remote build or deployment success. '
        'Verify the requested build host/directory, artifact checksum after transfer, installed/running version '
        'and actual target service health. Record real command results and failure/timeout diagnostics. '
        'Do not redeploy or rebuild an already completed stage just because a review or agent session restarts. '
        'Recheck its evidence and current remote state instead. '
        'Report an actual inaccessible host, failed authentication or missing external decision as blocked, '
        'with sanitized evidence; never claim an unexecuted step passed. '
    )
    if role == 'requirements':
        return common + (
            'This review remains strictly read-only; plan operational stages for the worker, do not perform deployment. '
            'An absent remote-check catalog or lack of a source-code function is not a blocker for this task. '
            'Ready means the worker can begin remote preflight, not that the environment or deployment is already verified.\n'
        )
    path = evidence_path(config.cwd, run_dir)
    example = {
        'run_id': Path(run_dir).name, 'requirement_sha256': requirement_hash(config.requirement),
        'source_version': '<actual source version>',
        'artifact': {'name': '<package or image>', 'sha256': '<64 lowercase hex digits>'},
        'target': {'host': '<requested deployment host>', 'install_dir': '<actual installation directory>',
                   'version': '<observed running version>'},
        'stages': [{'id': stage, 'status': 'passed', 'summary': '<observed result>',
                    'evidence': [stage + '.log']} for stage in STAGES],
    }
    common += ('\nDeployment evidence file: ' + str(path) + '\nJSON format: '
               + json.dumps(example, ensure_ascii=False)
               + '\nEvidence paths are relative to this JSON file; each must reference a nonempty local '
                 'sanitized command-result log (at most 1 MiB) inside this evidence directory. '
                 'Only build/package may be skipped when outside the original request; explain the reason in summary. '
                 'The reviewer must reject skipped stages that the person requested. '
                 'Source version, artifact SHA-256, target and observed running version are mandatory at final delivery. ')
    if role == 'worker':
        return common + (
            'Create/update this report and its logs from actual tool results. Include the report path in verdict evidence. '
            'During partial progress, preserve completed stages and report remaining stages honestly. '
            'Return requirement-level done only after remote verification, then hand off to independent review.\n'
        )
    return common + (
        'Review independently and read the referenced logs, comparing hosts, build directory, source/artifact version '
        'and verification against the original request. Read-only remote inspection is allowed where available; '
        'never build, install, restart services or perform other remote writes as reviewer. '
        'The watcher validates report structure and log presence, not the truth of remote observations. '
        'Do not accept self-reported success, an example report, local tests or an old deployment as proof. '
        'No source changes are required for acceptance.\n'
    )


def validate(cwd, run_dir, requirement):
    """Check a run-specific receipt; independent review still assesses its truth."""
    path = evidence_path(cwd, run_dir)
    base = path.parent.resolve()
    if not base.is_relative_to(Path(cwd).resolve()):
        raise ValueError('Deployment evidence directory must remain inside the project')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
        raise ValueError('Deployment evidence report is missing or invalid: ' + str(path))
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Deployment evidence report must be an object')
    if value.get('run_id') != Path(run_dir).name or value.get('requirement_sha256') != requirement_hash(requirement):
        raise ValueError('Deployment evidence belongs to a different run or requirement')
    def text(obj, key):
        item = obj.get(key) if isinstance(obj, dict) else None
        if not isinstance(item, str) or not item.strip() or len(item) > 4000:
            raise ValueError('Deployment evidence is missing ' + key)
        return item
    text(value, 'source_version')
    text(value.get('artifact'), 'name')
    if not re.fullmatch(r'[0-9a-f]{64}', text(value.get('artifact'), 'sha256')):
        raise ValueError('Deployment artifact requires an actual SHA-256 checksum')
    for field in ('host', 'install_dir', 'version'):
        text(value.get('target'), field)
    stages = value.get('stages')
    if (not isinstance(stages, list) or len(stages) != len(STAGES)
            or any(not isinstance(s, dict) for s in stages)
            or [s.get('id') for s in stages] != list(STAGES)):
        raise ValueError('Deployment evidence must cover preflight, build, package, deploy and verify in order')
    for stage in stages:
        text(stage, 'summary')
        if stage.get('status') == 'skipped' and stage['id'] in ('build', 'package'):
            continue
        if stage.get('status') != 'passed':
            raise ValueError('Deployment stage has not passed: ' + stage['id'])
        logs = stage.get('evidence')
        if not isinstance(logs, list) or not 1 <= len(logs) <= 20:
            raise ValueError('Deployment stage requires command-result logs: ' + stage['id'])
        for name in logs:
            if not isinstance(name, str) or not name or Path(name).is_absolute():
                raise ValueError('Deployment evidence log must be a relative path')
            log = base / name
            if (log.is_symlink() or not log.resolve().is_relative_to(base)
                    or not log.is_file() or not 0 < log.stat().st_size <= 1024 * 1024):
                raise ValueError('Deployment evidence log is missing, empty or outside the evidence directory')
    return value


def fingerprint(cwd, run_dir, requirement):
    """Bind candidate approval to the receipt and logs, including Git-ignored logs."""
    try:
        value = validate(cwd, run_dir, requirement)
        path = evidence_path(cwd, run_dir)
        hashes = {'result.json': hashlib.sha256(path.read_bytes()).hexdigest()}
        for stage in value['stages']:
            if stage['status'] == 'passed':
                for name in stage['evidence']:
                    hashes[name] = hashlib.sha256((path.parent / name).read_bytes()).hexdigest()
        return hashes
    except (OSError, ValueError) as exc:
        return {'error': str(exc)}
