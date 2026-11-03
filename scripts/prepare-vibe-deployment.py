#!/usr/bin/env python3
"""Create missing Vibe integration secrets in the private project .env."""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def main() -> None:
    if ENV_FILE.exists() and not stat.S_ISREG(ENV_FILE.stat().st_mode):
        raise SystemExit("项目根目录 .env 必须是普通文件。")
    original = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    lines = original.splitlines()
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")

    additions: list[str] = []
    token = values.get("VRA_API_TOKEN", "")
    if not token:
        token = secrets.token_urlsafe(48)
        additions.append(f"VRA_API_TOKEN={token}")
    elif len(token) < 32:
        raise SystemExit("现有 VRA_API_TOKEN 长度不足 32 字符；请在 .env 中更新它。")

    encryption_key = values.get("VRA_SETTINGS_ENCRYPTION_KEY", "")
    if not encryption_key:
        encryption_key = secrets.token_hex(32)
        additions.append(f"VRA_SETTINGS_ENCRYPTION_KEY={encryption_key}")
    elif len(encryption_key) != 64 or any(char not in "0123456789abcdefABCDEF" for char in encryption_key):
        raise SystemExit("现有 VRA_SETTINGS_ENCRYPTION_KEY 必须是 64 位十六进制字符。")

    if additions:
        content = original
        if content and not content.endswith("\n"):
            content += "\n"
        content += "\n# Vibe-Research host API integration\n" + "\n".join(additions) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=".env.vibe.", dir=ROOT)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, ENV_FILE)
        finally:
            try: os.unlink(temp_name)
            except FileNotFoundError: pass
    if ENV_FILE.exists():
        os.chmod(ENV_FILE, 0o600)
    print("Vibe 服务凭据已就绪，密钥保存在项目根目录 .env。")


if __name__ == "__main__":
    main()
