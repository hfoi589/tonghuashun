"""Shared fixed-AVD identity verification for deployment and lifecycle control."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Protocol


_SAFE_ADB_SERIAL = re.compile(r"[A-Za-z0-9._:-]+")
_SAFE_ADB_STATES = frozenset(
    {"bootloader", "device", "offline", "recovery", "sideload", "unauthorized"}
)


class IdentityVerificationError(RuntimeError):
    pass


class IdentityCommandRunner(Protocol):
    def run(
        self, args: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[bytes]: ...


class ProcessExecutableResolver(Protocol):
    def resolve(self, pid: int) -> Path: ...


class DarwinProcessExecutableResolver:
    _LIBPROC_PATH = "/usr/lib/libproc.dylib"
    _PROC_PIDPATHINFO_MAXSIZE = 4096

    def resolve(self, pid: int) -> Path:
        if sys.platform != "darwin" or pid <= 0:
            raise OSError("proc_pidpath unavailable")
        libproc = ctypes.CDLL(self._LIBPROC_PATH, use_errno=True)
        proc_pidpath = libproc.proc_pidpath
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(self._PROC_PIDPATHINFO_MAXSIZE)
        length = proc_pidpath(pid, buffer, len(buffer))
        if length <= 0 or length > len(buffer):
            raise OSError(ctypes.get_errno(), "proc_pidpath failed")
        raw_path = buffer.raw[:length].split(b"\0", 1)[0]
        if not raw_path:
            raise OSError("proc_pidpath returned an empty path")
        executable = Path(os.fsdecode(raw_path))
        if not executable.is_absolute():
            raise OSError("proc_pidpath returned a relative path")
        return executable


class FixedAvdPresence(str, Enum):
    ATTACHED = "ATTACHED"
    STARTING = "STARTING"
    ABSENT = "ABSENT"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class FixedAvdIdentity:
    presence: FixedAvdPresence
    adb_state: str | None = None


class FixedAvdIdentityVerifier:
    """Authenticate fixed serial/AVD/port bindings without argv0 trust."""

    def __init__(
        self,
        runner: IdentityCommandRunner,
        trusted_emulator_path: Path,
        process_executable_resolver: ProcessExecutableResolver,
        *,
        timeout_seconds: float = 15.0,
    ) -> None:
        try:
            trusted = trusted_emulator_path.resolve()
        except OSError:
            raise IdentityVerificationError() from None
        if not trusted.is_absolute() or trusted.name != "emulator":
            raise IdentityVerificationError()
        self._runner = runner
        self._trusted_emulator_path = trusted
        self._resolver = process_executable_resolver
        self._timeout_seconds = timeout_seconds

    def inspect(
        self,
        *,
        serial: str,
        expected_avd: str,
        emulator_port: int,
    ) -> FixedAvdIdentity:
        state_result = self._run(("adb", "-s", serial, "get-state"))
        if state_result.returncode == 0:
            state = self._text(state_result.stdout).strip()
            if state not in _SAFE_ADB_STATES:
                raise IdentityVerificationError()
            self.require_attached(serial=serial, expected_avd=expected_avd)
            return FixedAvdIdentity(FixedAvdPresence.ATTACHED, state)

        devices = self._adb_devices()
        listed_state = devices.get(serial)
        if listed_state is not None:
            self.require_attached(serial=serial, expected_avd=expected_avd)
            return FixedAvdIdentity(FixedAvdPresence.ATTACHED, listed_state)

        process = self._run(("ps", "-axo", "pid=,command="))
        if process.returncode != 0:
            raise IdentityVerificationError()
        starting = self._validate_starting_process(
            self._text(process.stdout), emulator_port, expected_avd
        )
        return FixedAvdIdentity(
            FixedAvdPresence.STARTING if starting else FixedAvdPresence.ABSENT
        )

    def require_attached(self, *, serial: str, expected_avd: str) -> None:
        result = self._run(("adb", "-s", serial, "emu", "avd", "name"))
        if result.returncode != 0:
            raise IdentityVerificationError()
        lines = [
            line.strip()
            for line in self._text(result.stdout).splitlines()
            if line.strip()
        ]
        if lines and lines[-1] == "OK":
            lines.pop()
        if lines != [expected_avd]:
            raise IdentityVerificationError()

    def _adb_devices(self) -> dict[str, str]:
        result = self._run(("adb", "devices"))
        if result.returncode != 0:
            raise IdentityVerificationError()
        lines = self._text(result.stdout).splitlines()
        if not lines or lines[0].strip() != "List of devices attached":
            raise IdentityVerificationError()
        devices: dict[str, str] = {}
        for raw_line in lines[1:]:
            line = raw_line.strip()
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) != 2:
                raise IdentityVerificationError()
            serial, state = (field.strip() for field in fields)
            if (
                _SAFE_ADB_SERIAL.fullmatch(serial) is None
                or state not in _SAFE_ADB_STATES
                or serial in devices
            ):
                raise IdentityVerificationError()
            devices[serial] = state
        return devices

    def _validate_starting_process(
        self, output: str, port: int, expected_avd: str
    ) -> bool:
        candidates: list[str] = []
        port_text = str(port)
        marker = re.compile(
            rf"(?:^|\s)-port(?:\s+|=){re.escape(port_text)}(?:\s|$)"
        )
        for raw_line in output.splitlines():
            parts = raw_line.strip().split(maxsplit=1)
            if len(parts) != 2 or re.fullmatch(r"[1-9][0-9]*", parts[0]) is None:
                if marker.search(raw_line):
                    raise IdentityVerificationError()
                continue
            pid = int(parts[0])
            try:
                tokens = shlex.split(parts[1], posix=True)
            except ValueError:
                if marker.search(parts[1]):
                    raise IdentityVerificationError() from None
                continue
            port_values = self._option_values(tokens, "-port")
            if port_text not in port_values:
                continue
            avd_values = self._option_values(tokens, "-avd")
            if port_values != [port_text] or avd_values != [expected_avd]:
                raise IdentityVerificationError()
            try:
                executable = self._resolver.resolve(pid).resolve()
            except Exception:
                raise IdentityVerificationError() from None
            if executable != self._trusted_emulator_path:
                raise IdentityVerificationError()
            candidates.append(avd_values[0])
        if len(candidates) > 1:
            raise IdentityVerificationError()
        return candidates == [expected_avd]

    @staticmethod
    def _option_values(tokens: list[str], option: str) -> list[str]:
        values: list[str] = []
        for index, token in enumerate(tokens):
            if token == option:
                if index + 1 >= len(tokens):
                    raise IdentityVerificationError()
                values.append(tokens[index + 1])
            elif token.startswith(f"{option}="):
                values.append(token.removeprefix(f"{option}="))
        return values

    def _run(self, args: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        try:
            return self._runner.run(args, self._timeout_seconds)
        except Exception:
            raise IdentityVerificationError() from None

    @staticmethod
    def _text(value: bytes | str | None) -> str:
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                raise IdentityVerificationError() from None
        return value or ""
