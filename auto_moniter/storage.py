"""Atomic writes for small status files without retaining old copies."""

import json
import os
import tempfile
from pathlib import Path


def write_json(path: Path, value: dict, *, durable: bool = False, mode: int | None = None) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=f".{path.name}-", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            if mode is not None and hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), mode)
            json.dump(value, handle, ensure_ascii=False)
            handle.write("\n")
            if durable:
                handle.flush()
                os.fsync(handle.fileno())
        if mode is not None and not hasattr(os, "fchmod"):
            os.chmod(temporary, mode)
        temporary.replace(path)
        if durable and hasattr(os, "O_DIRECTORY"):
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
