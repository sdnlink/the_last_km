"""Source-checkout entry point for the read-only NDR documentation gate."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_moniter.documentation import main

if __name__ == '__main__':
    raise SystemExit(main())
