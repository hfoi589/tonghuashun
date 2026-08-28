#!/usr/bin/env python3
"""Create the only secrets stored in the deployment env file."""

from __future__ import annotations

import argparse
import base64
import getpass
import os
import secrets
import stat
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError
from argon2.low_level import Type


_NAME = __import__("re").compile(r"[A-Z][A-Z0-9_]*")
_LIFECYCLE_TOKEN = __import__("re").compile(r"[A-Za-z0-9_-]{32,}")
_MAX_ENV_BYTES = 256 * 1024


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_existing(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("invalid env line")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not _NAME.fullmatch(name) or name in values:
            raise ValueError("invalid env name")
        values[name] = _unquote(raw_value)
    return values


def _valid_session_key(value: str) -> bool:
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    except (TypeError, ValueError):
        return False
    return len(decoded) == 32


def _validate_upgrade_values(values: dict[str, str]) -> None:
    password_hash = values.get("ADMIN_PASSWORD_HASH", "")
    session_secret = values.get("ADMIN_SESSION_SECRET", "")
    try:
        PasswordHasher(type=Type.ID).check_needs_rehash(password_hash)
    except InvalidHashError:
        raise ValueError("invalid admin hash") from None
    if len(session_secret) < 32:
        raise ValueError("invalid session secret")
    encryption_key = values.get("THS_SESSION_ENCRYPTION_KEY")
    if encryption_key is not None and not _valid_session_key(encryption_key):
        raise ValueError("invalid session encryption key")
    lifecycle_token = values.get("THS_DEVICE_LIFECYCLE_TOKEN")
    if lifecycle_token is not None and not _LIFECYCLE_TOKEN.fullmatch(
        lifecycle_token
    ):
        raise ValueError("invalid lifecycle token")


def _read_secure_existing(path: Path) -> str:
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_ENV_BYTES
    ):
        raise ValueError("unsafe env metadata")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        current = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_uid != os.getuid()
            or current.st_size != metadata.st_size
        ):
            raise ValueError("unsafe env metadata")
        raw = handle.read(_MAX_ENV_BYTES + 1)
    if len(raw) > _MAX_ENV_BYTES:
        raise ValueError("env too large")
    return raw.decode("utf-8")


def _atomic_upgrade(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _upgrade_existing(path: Path) -> int:
    try:
        content = _read_secure_existing(path)
        values = _parse_existing(content)
        _validate_upgrade_values(values)
        additions: list[str] = []
        if "THS_SESSION_ENCRYPTION_KEY" not in values:
            additions.append(
                "THS_SESSION_ENCRYPTION_KEY="
                + base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
            )
        if "THS_DEVICE_LIFECYCLE_TOKEN" not in values:
            additions.append(
                "THS_DEVICE_LIFECYCLE_TOKEN=" + secrets.token_urlsafe(32)
            )
        if additions:
            separator = "" if content.endswith("\n") else "\n"
            _atomic_upgrade(path, content + separator + "\n".join(additions) + "\n")
    except Exception:
        print("Existing deployment env is invalid; no changes were made.", file=sys.stderr)
        return 2
    print("Existing deployment env is ready; secret values were not printed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upgrade-existing",
        action="store_true",
        help="atomically add only missing post-feature deployment secrets",
    )
    parser.add_argument("env_file", type=Path, help="new 0600 file to create, for example .env")
    args = parser.parse_args(argv)
    if args.upgrade_existing:
        return _upgrade_existing(args.env_file)
    password = getpass.getpass("Admin password: ")
    confirmation = getpass.getpass("Confirm admin password: ")
    if not password:
        print("Admin password must not be empty", file=sys.stderr)
        return 2
    if password != confirmation:
        print("Passwords did not match", file=sys.stderr)
        return 2
    args.env_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(args.env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"Refusing to overwrite existing secret file: {args.env_file}", file=sys.stderr)
        return 2
    password_hash = PasswordHasher(type=Type.ID).hash(password)
    session_encryption_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    # Compose interpolates unquoted `$name` sequences while loading `.env`.
    # Single quotes keep the Argon2id separators literal without persisting
    # the administrator's plaintext password.
    content = (
        f"ADMIN_PASSWORD_HASH='{password_hash}'\n"
        f"ADMIN_SESSION_SECRET={secrets.token_urlsafe(48)}\n"
        f"THS_SESSION_ENCRYPTION_KEY={session_encryption_key}\n"
        f"THS_DEVICE_LIFECYCLE_TOKEN={secrets.token_urlsafe(32)}\n"
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
    os.chmod(args.env_file, 0o600)
    print(f"Created {args.env_file} with mode 0600; no app credentials were written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
