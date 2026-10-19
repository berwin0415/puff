#!/usr/bin/env bash
set -euo pipefail

command -v docker >/dev/null
command -v uv >/dev/null
docker info >/dev/null
docker compose version
test -x .venv/bin/python
.venv/bin/python -c 'import httpx, pydantic, yaml, playwright; print("python baseline ok")'
echo "local harness environment ok"
