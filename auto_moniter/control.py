"""Read bounded control requests without treating damaged input as a command."""
import json
from pathlib import Path


class ControlError(ValueError):
    """The request exists but cannot currently be read or understood."""


def _unique_fields(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ControlError('Duplicate control request field')
        fields[key] = value
    return fields


def read_control(path: Path) -> dict:
    try:
        with path.open('rb') as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ControlError('Cannot read control request: ' + str(exc)) from exc
    if len(raw) > 4096:
        raise ControlError('Control request exceeds 4096 bytes')
    try:
        value = json.loads(raw, object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError) as exc:
        raise ControlError('Control request is not valid JSON') from exc
    if (not isinstance(value, dict) or not isinstance(value.get('action'), str)
            or value['action'] not in {'pause', 'stop', 'continue'}):
        raise ControlError('Control request action must be pause, stop or continue')
    return value
