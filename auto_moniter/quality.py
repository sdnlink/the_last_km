"""Monitor-owned requirements, immutable test baselines and verified progress."""
import fnmatch
import json
import shlex
import re
from pathlib import Path

from auto_moniter.workspace import file_snapshot


DEFAULTS = {'protect_tests': True, 'verified_progress': True, 'no_progress_limit': 12,
            'review_interval_s': 600, 'review_changed_files': 20, 'blocked_review_limit': 3,
            'requirements': [], 'validation_paths': [], 'review_tools': 'provider',
            'max_tokens': 0, 'max_cost_usd': 0}


def policy(value):
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise ValueError('Unknown or invalid quality policy field')
    result = {**DEFAULTS, **value}
    if len(json.dumps(result, ensure_ascii=False).encode()) > 128 * 1024:
        raise ValueError('Quality policy exceeds 128 KiB')
    for key in ('protect_tests', 'verified_progress'):
        if type(result[key]) is not bool:
            raise ValueError(key + ' must be boolean')
    for key in ('no_progress_limit', 'review_interval_s', 'review_changed_files', 'blocked_review_limit', 'max_tokens'):
        if type(result[key]) is not int or not 0 <= result[key] <= 10**12:
            raise ValueError(key + ' must be a nonnegative integer')
    cost = result['max_cost_usd']
    if type(cost) not in (int, float) or not 0 <= cost <= 10**9:
        raise ValueError('max_cost_usd must be finite and nonnegative')
    if result['review_tools'] not in ('provider', 'files_only'):
        raise ValueError('review_tools must be provider or files_only')
    paths = result['validation_paths']
    if not isinstance(paths, list) or len(paths) > 100:
        raise ValueError('validation_paths must be a bounded list')
    for path in paths:
        relative_path(path)
    requirements = result['requirements']
    if not isinstance(requirements, list) or len(requirements) > 200:
        raise ValueError('requirements must be a list with at most 200 entries')
    seen = set()
    for req in requirements:
        if (not isinstance(req, dict) or set(req) != {'id', 'text', 'module_ids', 'evidence_paths'}
                or not isinstance(req['id'], str) or not 1 <= len(req['id']) <= 64
                or req['id'] in seen or not isinstance(req['text'], str) or not 1 <= len(req['text']) <= 2000):
            raise ValueError('Each unique requirement needs id, text, module_ids and evidence_paths')
        for key in ('module_ids', 'evidence_paths'):
            if (not isinstance(req[key], list) or not 1 <= len(req[key]) <= 30
                    or any(not isinstance(v, str) or not v for v in req[key])):
                raise ValueError('Requirement needs nonempty module_ids and evidence_paths')
        for path in req['evidence_paths']:
            relative_path(path)
        seen.add(req['id'])
    return result


def relative_path(value):
    if not isinstance(value, str) or not value or '\0' in value or Path(value).is_absolute() or '..' in Path(value).parts:
        raise ValueError('Policy paths must stay inside the project')


def test_asset(relative):
    p = Path(relative)
    return (any(part in {'tests', 'test', '__tests__', '.github'} for part in p.parts)
            or p.name in {'conftest.py', 'pytest.ini', 'tox.ini', '.coveragerc', 'pyproject.toml', 'package.json', 'Makefile', '.gitignore'}
            or p.name.startswith('test_') or '.test.' in p.name or '.spec.' in p.name)


def cited(paths, entries):
    return any(re.search(r'(?<![\w./-])' + re.escape(path) + r'(?![\w./-])', entry)
               for path in paths for entry in entries)


class Quality:
    def __init__(self, config, baseline, saved=None):
        self.config = policy(config) if config else {}
        self.state = saved or {'milestones': [], 'checks': {}, 'blocker_key': '', 'blocker_count': 0}
        self.baseline = baseline
        self.command_assets = set()
        if (not isinstance(self.state, dict) or set(self.state) != {'milestones', 'checks', 'blocker_key', 'blocker_count'}
                or not isinstance(self.state['milestones'], list) or len(self.state['milestones']) > 1000
                or any(not isinstance(k, str) for k in self.state['milestones'])
                or not isinstance(self.state['checks'], dict) or len(self.state['checks']) > 1000
                or type(self.state['blocker_count']) is not int or self.state['blocker_count'] < 0
                or not isinstance(self.state['blocker_key'], str)
                or any(not isinstance(k, str) or type(v) is not bool for k, v in self.state['checks'].items())):
            raise ValueError('Invalid saved quality state')

    def bind_checks(self, project, commands):
        for command in commands:
            for token in shlex.split(command):
                path = (project / token).resolve()
                if path.is_relative_to(project) and str(path) in self.baseline:
                    self.command_assets.add(str(path))

    def protected(self, project):
        if not self.config:
            return {}
        selected = {}
        for name, token in self.baseline.items():
            relative = str(Path(name).relative_to(project))
            if (name in self.command_assets or self.config['protect_tests'] and test_asset(relative)
                    or any(relative == p or relative.startswith(p.rstrip('/') + '/') for p in self.config['validation_paths'])):
                selected[name] = token
        return selected

    def baseline_errors(self, project, current=None):
        current = current if current is not None else file_snapshot(project)
        return [str(Path(name).relative_to(project)) for name, token in self.protected(project).items()
                if current.get(name) != token]

    def check_result(self, command, success):
        old = self.state['checks'].get(command)
        self.state['checks'][command] = bool(success)
        return self.milestone('check:' + command) if success and old is not True else False

    def milestone(self, key):
        if key in self.state['milestones']:
            return False
        if len(self.state['milestones']) >= 1000:
            return False
        self.state['milestones'].append(key)
        return True

    def coverage(self, project, modules, worker, reviewer):
        rows, errors = [], []
        joint = set(worker.module_ids) & set(reviewer.module_ids)
        current = file_snapshot(project)
        for req in self.config.get('requirements', []):
            start_errors = len(errors)
            missing = [key for key in req['module_ids'] if key not in modules.items or
                       not (modules.items[key]['status'] == 'accepted' or key in joint)]
            evidence = []
            for pattern in req['evidence_paths']:
                found = [str(Path(p).relative_to(project)) for p in current
                         if fnmatch.fnmatchcase(str(Path(p).relative_to(project)), pattern)
                         and Path(p).is_file()
                         and (project / Path(p).relative_to(project)).resolve().is_relative_to(project)]
                if not found:
                    errors.append(f"Requirement {req['id']} has no file evidence matching {pattern}")
                elif not (all(cited(found, v.evidence) for v in (worker, reviewer))
                          or all(modules.items.get(key, {}).get('status') == 'accepted'
                                 and cited(found, modules.items[key].get('evidence', []))
                                 for key in req['module_ids'])):
                    errors.append(f"Requirement {req['id']} lacks joint cited evidence for {pattern}")
                evidence.extend(found[:30])
            if missing:
                errors.append(f"Requirement {req['id']} has unverified modules: {', '.join(missing)}")
            rows.append({'id': req['id'], 'text': req['text'], 'module_ids': req['module_ids'],
                         'evidence': sorted(set(evidence))[:60], 'covered': len(errors) == start_errors})
        return rows, errors

    def review_blockers(self, blockers):
        key = json.dumps(sorted(set(blockers)), ensure_ascii=False) if blockers else ''
        self.state['blocker_count'] = self.state['blocker_count'] + 1 if key and key == self.state['blocker_key'] else int(bool(key))
        self.state['blocker_key'] = key
        return bool(self.config.get('blocked_review_limit') and self.state['blocker_count'] >= self.config['blocked_review_limit'])
