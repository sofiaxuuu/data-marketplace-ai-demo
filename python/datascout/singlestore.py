"""SingleStore connection adapter and a read-only connectivity check.

Run with: PYTHONPATH=python .venv/bin/python -m datascout.singlestore check
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import singlestoredb as s2
import certifi
from dotenv import load_dotenv

from .catalog import ROOT


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    ssl_ca: str | None = None

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> Settings:
        if values is None:
            load_dotenv(ROOT / ".env", override=False)
            values = os.environ
        names = ("HOST", "DATABASE", "USER", "PASSWORD")
        missing = [f"SINGLESTORE_{name}" for name in names if not values.get(f"SINGLESTORE_{name}")]
        if missing:
            raise ConfigurationError("Set " + ", ".join(missing) + " in the local .env file.")
        try:
            port = int(values.get("SINGLESTORE_PORT", "3333"))
        except ValueError as error:
            raise ConfigurationError("SINGLESTORE_PORT must be an integer.") from error
        if not 1 <= port <= 65535:
            raise ConfigurationError("SINGLESTORE_PORT must be between 1 and 65535.")
        ssl_ca = values.get("SINGLESTORE_SSL_CA")
        if ssl_ca:
            ca_path = Path(ssl_ca).expanduser()
            if not ca_path.is_absolute():
                ca_path = ROOT / ca_path
            if not ca_path.is_file():
                raise ConfigurationError("SINGLESTORE_SSL_CA must point to an existing certificate bundle.")
            ssl_ca = str(ca_path.resolve())
        return cls(
            host=values["SINGLESTORE_HOST"],
            port=port,
            database=values["SINGLESTORE_DATABASE"],
            user=values["SINGLESTORE_USER"],
            password=values["SINGLESTORE_PASSWORD"],
            ssl_ca=ssl_ca,
        )


def connect(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    # Separate arguments preserve spaces and special characters without URL encoding.
    return s2.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        database=settings.database,
        ssl_disabled=False,
        ssl_ca=settings.ssl_ca or certifi.where(),
        ssl_verify_cert=True,
        ssl_verify_identity=True,
        connect_timeout=10,
        local_infile=False,
        multi_statements=False,
        pure_python=True,
        autocommit=True,
    )


def check_connection() -> dict[str, object]:
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE(), VERSION(), 1")
            database, version, probe = cursor.fetchone()
            return {"connected": probe == 1, "database": database, "server_version": version}


def connection_error_message(error: Exception) -> str:
    """Classify driver errors using fixed messages without exposing their contents."""
    text = str(error).lower()
    code = error.args[0] if error.args and isinstance(error.args[0], int) else None
    if code == 1045 or "access denied" in text:
        return "Authentication rejected. Check SINGLESTORE_USER and SINGLESTORE_PASSWORD in .env."
    if "hostname mismatch" in text or "doesn't match" in text:
        return "TLS hostname mismatch. Check SINGLESTORE_HOST against the endpoint shown by SingleStore."
    if "certificate verify failed" in text:
        return "TLS certificate verification failed. Check SINGLESTORE_SSL_CA and the downloaded certificate bundle."
    if code == 1049 or "unknown database" in text:
        return "Database not found. Check SINGLESTORE_DATABASE in .env."
    if "timed out" in text:
        return "Connection timed out. Check endpoint availability and network access."
    if "nodename nor servname" in text or "name or service not known" in text:
        return "Endpoint DNS lookup failed. Check SINGLESTORE_HOST and network access."
    return "SingleStore connection failed. Check endpoint access and configuration."


def main() -> None:
    parser = argparse.ArgumentParser(description="DataScout SingleStore setup")
    parser.add_argument("command", choices=["check"])
    parser.parse_args()
    try:
        result = check_connection()
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as error:
        print(connection_error_message(error), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
