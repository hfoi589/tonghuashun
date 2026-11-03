#!/usr/bin/env python3
"""Launch the Vibe API on macOS with only its dedicated secrets in the environment."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VIBE_ROOT = ROOT / "vibe_research"
SECRET_KEYS = {"VRA_API_TOKEN", "VRA_SETTINGS_ENCRYPTION_KEY"}


def read_private_values() -> dict[str, str]:
    env_file = ROOT / ".env"
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key in SECRET_KEYS:
            value = value.strip().strip('"').strip("'")
            values[key] = value
    token = values.get("VRA_API_TOKEN", "")
    encryption_key = values.get("VRA_SETTINGS_ENCRYPTION_KEY", "")
    if len(token) < 32 or not re.fullmatch(r"[A-Fa-f0-9]{64}", encryption_key):
        raise SystemExit("Vibe 主机服务密钥配置无效，请检查项目根目录 .env")
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    env = os.environ.copy()
    env.update(read_private_values())
    venv_bin = VIBE_ROOT / ".venv" / "bin"
    env["VRA_DATA_ROOT"] = str(VIBE_ROOT / ".local")
    env["VRA_PYTHON"] = str(venv_bin / "python")
    env["VRA_MARKET_API_URL"] = "http://127.0.0.1:8001"
    old_path = env.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    env["PATH"] = os.pathsep.join([str(venv_bin), str(Path(args.node).parent), old_path])
    os.chdir(VIBE_ROOT)
    command = [args.node, "orchestrator/src/api.ts", "--host", args.host, "--port", str(args.port)]
    subprocess.run(command, env=env, check=False)


if __name__ == "__main__":
    main()
