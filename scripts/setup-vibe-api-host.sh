#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

command -v uv >/dev/null 2>&1 || { echo "[vibe] 未找到 uv。" >&2; exit 1; }
command -v node >/dev/null 2>&1 || { echo "[vibe] 未找到 Node.js 22.18 或更高版本。" >&2; exit 1; }
node "$ROOT/vibe_research/scripts/check-node.mjs"

uv python install 3.12
uv venv --python 3.12 "$ROOT/vibe_research/.venv"
uv pip install --python "$ROOT/vibe_research/.venv/bin/python" \
  -r "$ROOT/vibe_research/.agents/skills/data-access/scripts/requirements.lock.txt"
npm ci --prefix "$ROOT/vibe_research/orchestrator" --no-audit --no-fund
python3 "$ROOT/scripts/prepare-vibe-deployment.py"
node "$ROOT/vibe_research/orchestrator/src/init.ts" --python "$ROOT/vibe_research/.venv/bin/python"

echo "[vibe] 主机 API 依赖与私有数据目录已准备。部署 API 后运行 scripts/install-vibe-api-launch-agent.py。"
