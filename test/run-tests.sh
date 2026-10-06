#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec env PYTHONPATH="plugin/scripts" python3 -m unittest discover -s test -t . -v "$@"
