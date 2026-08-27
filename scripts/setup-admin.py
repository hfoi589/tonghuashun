#!/usr/bin/env python3
"""Create the only secrets stored in the deployment env file."""

from __future__ import annotations

import argparse
import base64
import getpass
import os
import secrets
import sys
from pathlib import Path

from argon2 import PasswordHasher
from argon2.low_level import Type


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", type=Path, help="new 0600 file to create, for example .env")
    args = parser.parse_args(argv)
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
