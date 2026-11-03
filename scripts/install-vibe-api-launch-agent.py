#!/usr/bin/env python3
"""Install/reload the Vibe API LaunchAgent for OrbStack host-loopback access."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.ths.vibe-api"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library" / "Logs" / "ths-vibe-api.log"
DEFAULT_API_PORT = 8765


def docker(compose_args: list[str], *, check: bool = True) -> str:
    command = [
        "docker", "--context", "orbstack", "compose",
        "--project-name", "ths-level2",
        "--env-file", ".env",
        "--env-file", "deploy/macos.env",
        "-f", "deploy/compose.yml",
        *compose_args,
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise SystemExit("OrbStack compose 操作失败；请先确认 API 服务已部署并运行。")
    return result.stdout.strip()


def host_bind_address() -> str:
    """OrbStack forwards host.docker.internal to the host's loopback listener.

    The address returned for host.docker.internal inside the container is a
    synthetic gateway address, not a local macOS interface that can be bound.
    """
    return "127.0.0.1"


def running_api_container() -> str:
    container = docker(["ps", "-q", "api"])
    if not container:
        raise SystemExit("没有运行中的 ths-level2 API 容器；先执行标准 OrbStack compose 部署命令。")
    return container


def configured_agent_port() -> int | None:
    try:
        with PLIST.open("rb") as file:
            arguments = plistlib.load(file).get("ProgramArguments", [])
        return int(arguments[arguments.index("--port") + 1])
    except (OSError, ValueError, IndexError, TypeError):
        return None


def node_binary() -> str:
    found = shutil.which("node")
    if not found:
        raise SystemExit("未找到 Node.js；Vibe Research 需要 Node.js 22.18 或更高版本。")
    version = subprocess.run([found, "--version"], text=True, capture_output=True, check=True).stdout.strip()
    nums = tuple(int(part) for part in version.removeprefix("v").split(".")[:2])
    if nums < (22, 18):
        raise SystemExit(f"Node.js 版本 {version} 低于 Vibe Research 要求的 22.18。")
    return found


def load_token() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("VRA_API_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(".env 缺少 VRA_API_TOKEN。")


def launchctl(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], text=True, capture_output=True, check=check)


def wait_for_health(host: str, token: str, port: int, *, attempts: int = 30) -> bool:
    url = f"http://{host}:{port}/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(attempts):
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with opener.open(request, timeout=2) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    return False


def port_is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def select_api_port(token: str) -> int:
    current = configured_agent_port()
    if current is not None and wait_for_health("127.0.0.1", token, current, attempts=1):
        return current
    for port in range(DEFAULT_API_PORT, DEFAULT_API_PORT + 32):
        if port_is_available(port):
            return port
    raise SystemExit("8765–8796 端口都不可用，无法启动 Vibe API。")


def set_compose_backend_url(port: int) -> bool:
    env_file = ROOT / ".env"
    original = env_file.read_text(encoding="utf-8")
    desired = f"VRA_BACKEND_URL=http://host.docker.internal:{port}"
    lines = original.splitlines()
    found = False
    updated_lines: list[str] = []
    for line in lines:
        if line.startswith("VRA_BACKEND_URL="):
            updated_lines.append(desired)
            found = True
        else:
            updated_lines.append(line)
    if not found:
        updated_lines.append(desired)
    updated = "\n".join(updated_lines) + "\n"
    if updated == original:
        return False

    mode = stat.S_IMODE(env_file.stat().st_mode)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=ROOT, prefix=".env.", delete=False) as file:
        temporary = Path(file.name)
        file.write(updated)
    os.chmod(temporary, mode)
    os.replace(temporary, env_file)
    return True


def wait_for_container_health(container: str, token: str, port: int) -> bool:
    code = (
        "import sys, urllib.request; "
        "token=sys.stdin.read(); "
        f"request=urllib.request.Request('http://host.docker.internal:{port}/health', "
        "headers={'Authorization':'Bearer '+token}); "
        "response=urllib.request.urlopen(request, timeout=2); "
        "print(response.status)"
    )
    command = ["docker", "--context", "orbstack", "exec", "-i", container, "python", "-c", code]
    for _ in range(30):
        result = subprocess.run(command, cwd=ROOT, input=token, text=True, capture_output=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip() == "200":
            return True
        time.sleep(1)
    return False


def wait_for_queue_idle() -> None:
    for _ in range(300):
        count = docker(["exec", "redis", "redis-cli", "--raw", "get", "ths:jobs:active-count"], check=False).strip()
        if not count or count == "0":
            return
        time.sleep(1)
    raise SystemExit("仍有运行中的任务；API 配置尚未变更，请等任务结束后重试。")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    uid = os.getuid()
    domain = f"gui/{uid}"

    if args.uninstall:
        launchctl(["bootout", domain, str(PLIST)], check=False)
        PLIST.unlink(missing_ok=True)
        print("Vibe API LaunchAgent 已卸载。数据目录保留。")
        return

    venv_python = ROOT / "vibe_research" / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        raise SystemExit("Vibe Python 环境未安装；请先运行 Vibe 主机依赖安装步骤。")
    node = node_binary()
    container = running_api_container()
    host = host_bind_address()
    token = load_token()
    port = select_api_port(token)
    backend_changed = set_compose_backend_url(port)
    if backend_changed:
        wait_for_queue_idle()
        docker(["up", "-d", "api"])
        container = running_api_container()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable,
            str(ROOT / "scripts" / "run-vibe-api-host.py"),
            "--node", node,
            "--host", host,
            "--port", str(port),
        ],
        "WorkingDirectory": str(ROOT / "vibe_research"),
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")},
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }
    with PLIST.open("wb") as file:
        plistlib.dump(payload, file)
    os.chmod(PLIST, 0o600)
    launchctl(["bootout", domain, str(PLIST)], check=False)
    launchctl(["bootstrap", domain, str(PLIST)])
    launchctl(["kickstart", "-k", f"{domain}/{LABEL}"])
    if not wait_for_health(host, token, port):
        raise SystemExit(f"Vibe API 未通过健康检查；请查看 {LOG}。密钥未写入 LaunchAgent 日志或 plist。")
    if not wait_for_container_health(container, token, port):
        raise SystemExit("Vibe API 主机健康，但 API 容器未能通过 host.docker.internal 访问；请检查 OrbStack 网络。")
    print(f"Vibe API LaunchAgent 已就绪，监听主机回环地址 {host}:{port}；容器通过 host.docker.internal 访问。")


if __name__ == "__main__":
    main()
