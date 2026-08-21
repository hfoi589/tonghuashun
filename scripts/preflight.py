#!/usr/bin/env python3
"""Reject unsupported deployment hosts and incorrect THS APK artifacts early."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile


EXPECTED_APK_SHA256 = "2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e"
ARM_ABIS = ("arm64-v8a", "armeabi-v7a")
MINIMUM_CPU_COUNT = 4
MINIMUM_MEMORY_BYTES = 8 << 30
MINIMUM_FREE_BYTES = 30 << 30


class PreflightError(ValueError):
    """A host or artifact does not meet the explicitly supported profile."""


@dataclass(frozen=True)
class ApkInspection:
    path: Path
    sha256: str
    abis: tuple[str, ...]


def validate_apk(path: Path, *, expected_sha256: str = EXPECTED_APK_SHA256) -> ApkInspection:
    """Validate the exact artifact and ensure it has a native ARM implementation."""
    if not path.is_file():
        raise PreflightError(f"APK not found: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise PreflightError(f"APK SHA-256 mismatch: expected {expected_sha256}, got {digest}")
    try:
        with ZipFile(path) as archive:
            abis = tuple(sorted({name.split("/")[1] for name in archive.namelist() if name.startswith("lib/") and name.count("/") >= 2}))
    except BadZipFile as error:
        raise PreflightError(f"APK is not a readable zip archive: {path}") from error
    if not set(abis).intersection(ARM_ABIS):
        raise PreflightError(f"APK has no supported ARM ABI ({', '.join(ARM_ABIS)}); found: {', '.join(abis) or 'none'}")
    return ApkInspection(path=path, sha256=digest, abis=abis)


def validate_host_profile(
    profile: str,
    *,
    architecture: str,
    cpu_count: int,
    memory_bytes: int,
    free_bytes: int,
    docker_available: bool = False,
    docker_rootless: bool = False,
    binder_available: bool = False,
    apple_silicon: bool = False,
    android_sdk_available: bool = False,
    avd_available: bool = False,
) -> None:
    """Check only the two documented deployment profiles, never a generic VPS."""
    errors: list[str] = []
    normalized_arch = architecture.lower()
    if cpu_count < MINIMUM_CPU_COUNT:
        errors.append("at least 4 CPU cores are required")
    if memory_bytes < MINIMUM_MEMORY_BYTES:
        errors.append("at least 8 GiB RAM is required")
    if free_bytes < MINIMUM_FREE_BYTES:
        errors.append("at least 30 GiB free disk is required")
    if profile == "linux-redroid":
        if normalized_arch not in {"amd64", "x86_64"}:
            errors.append("linux-redroid is supported only on amd64")
        if not docker_available:
            errors.append("Docker is required for linux-redroid")
        if docker_rootless:
            errors.append("rootless Docker cannot provide Redroid privileged Binder access")
        if not binder_available:
            errors.append("Binder devices or binderfs are required for linux-redroid")
    elif profile == "macos-avd":
        if normalized_arch not in {"arm64", "aarch64"} or not apple_silicon:
            errors.append("macos-avd requires Apple Silicon (arm64)")
        if not android_sdk_available:
            errors.append("Android SDK platform-tools, emulator, and API 33 image are required")
        if not avd_available:
            errors.append("the configured API 33 ARM64 AVD is required")
    else:
        errors.append("profile must be linux-redroid or macos-avd; arbitrary VPS hosts are unsupported")
    if errors:
        raise PreflightError("; ".join(errors))


def _command_available(name: str) -> bool:
    return shutil.which(name) is not None


def _memory_bytes() -> int:
    if sys.platform == "darwin":
        return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _docker_rootless() -> bool:
    if not _command_available("docker"):
        return False
    try:
        output = subprocess.check_output(["docker", "info", "--format", "{{json .SecurityOptions}}"], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return True
    return "rootless" in output.lower()


def _binder_available() -> bool:
    return all(Path(path).exists() for path in ("/dev/binderfs", "/dev/binder", "/dev/hwbinder", "/dev/vndbinder"))


def _mac_sdk_available() -> bool:
    return all(_command_available(binary) for binary in ("adb", "emulator", "sdkmanager", "avdmanager"))


def _avd_available(name: str) -> bool:
    if not _command_available("emulator"):
        return False
    try:
        output = subprocess.check_output(["emulator", "-list-avds"], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return False
    return name in output.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("linux-redroid", "macos-avd"))
    parser.add_argument("--apk", required=True, type=Path)
    parser.add_argument("--avd-name", default="THS_API_33_ARM64")
    parser.add_argument("--apk-only", action="store_true", help="validate the external artifact before Android SDK bootstrap")
    args = parser.parse_args(argv)
    try:
        apk = validate_apk(args.apk)
        if not args.apk_only:
            if not args.profile:
                raise PreflightError("--profile is required unless --apk-only is used")
            usage = shutil.disk_usage(Path.cwd())
            validate_host_profile(
                args.profile,
                architecture=platform.machine(),
                cpu_count=os.cpu_count() or 0,
                memory_bytes=_memory_bytes(),
                free_bytes=usage.free,
                docker_available=_command_available("docker"),
                docker_rootless=_docker_rootless(),
                binder_available=_binder_available(),
                apple_silicon=sys.platform == "darwin" and platform.machine().lower() in {"arm64", "aarch64"},
                android_sdk_available=_mac_sdk_available(),
                avd_available=_avd_available(args.avd_name),
            )
    except PreflightError as error:
        print(f"PREFLIGHT FAILED: {error}", file=sys.stderr)
        return 2
    scope = "apk-only" if args.apk_only else args.profile
    print(f"PREFLIGHT OK: profile={scope} apk={apk.path} sha256={apk.sha256} abis={','.join(apk.abis)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
