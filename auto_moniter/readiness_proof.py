"""Verify preparation receipts with a public key registered outside the project."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

from auto_moniter import requirement_context

ENV = 'AUTO_MONITOR_READINESS_TRUST_DIRECTORY'
CONFIG = 'dispatch-readiness-executors.json'
NAMESPACE = 'auto-moniter-readiness'


def signing_bytes(payload):
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode() + b'\n'


def verified(root, spec, payload):
    directory = os.environ.get(ENV)
    verifier = shutil.which('ssh-keygen', path=os.defpath)
    if not verifier or not directory or not Path(directory).is_absolute():
        return False
    try:
        config = Path(directory).resolve() / CONFIG
        if config.is_relative_to(root):
            return False
        registered = requirement_context.read(config, limit=128 * 1024)['executors']
        if not isinstance(registered, list) or len(registered) > 100:
            return False
        keys = [row['public_key'] for row in registered if row['workspace'] == str(root)
                and row['environment'] == spec['environment'] and row['executor'] == spec['executor']
                and isinstance(row['resource'], str) and row['resource'].strip()
                and row['resource'] == payload.get('resource')]
        if (len(keys) != 1 or not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]{68}', keys[0])
                or payload.get('workspace') != str(root) or payload.get('path') != spec['path']):
            return False
        signature = payload['signature']
        if not isinstance(signature, str) or not 0 < len(signature) <= 2048:
            return False
        signed = {key: value for key, value in payload.items() if key != 'signature'}
        with TemporaryDirectory(prefix='auto-moniter-readiness-') as temporary:
            allowed = Path(temporary) / 'allowed_signers'
            proof = Path(temporary) / 'receipt.sig'
            allowed.write_text(f'executor {keys[0]}\n', encoding='ascii')
            proof.write_text(signature, encoding='ascii')
            result = subprocess.run(
                [verifier, '-Y', 'verify', '-f', str(allowed), '-I', 'executor',
                 '-n', NAMESPACE, '-s', str(proof)],
                input=signing_bytes(signed), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False)
        return result.returncode == 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired):
        return False
