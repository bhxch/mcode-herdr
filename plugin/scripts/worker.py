#!/usr/bin/env python3
"""后台 worker：真正执行上报。由 herdr-report.py 派生。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from mcode_herdr.runtime import main  # noqa: E402


if __name__ == "__main__":
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        main([event], sys.stdin, os.environ)
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)
