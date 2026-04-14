#!/usr/bin/env bash
# Convenience wrapper. Same as `./run.sh --cleanup`.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec ./run.sh --cleanup
