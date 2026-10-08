"""Make pytest offline and keep its default database inside a temporary directory."""

import os
from pathlib import Path
import socket
import sqlite3
import threading
import tempfile

import pytest
import requests

# This executes before test collection imports application modules.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
for name in ("TURSO_DB_URL", "TURSO_AUTH_TOKEN", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "FIREWORKS_API_KEY",
             "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_OPERATOR_ID", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
    os.environ[name] = ""
os.environ["LANGFUSE_ENABLED"] = "false"
os.environ["LLM_PROVIDER"] = ""
os.environ.pop("LLM_MODEL", None)


@pytest.fixture(autouse=True)
def isolate_runtime(tmp_path, monkeypatch):
    """Tests can replace these mocks explicitly, but cannot fall through to live I/O."""
    from src import db

    # Keep unittest TemporaryDirectory users inside the same writable test root.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    database = tmp_path / "default-test.db"
    con = sqlite3.connect(database)
    con.executescript((Path(__file__).resolve().parents[1] / "data" / "schema.sql").read_text(encoding="utf-8"))
    con.close()
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "TURSO_URL", None)
    monkeypatch.setattr(db, "TURSO_TOKEN", None)
    monkeypatch.setattr(db, "_SQLITE_SCHEMA_CHECKED", False)
    monkeypatch.setattr(db, "_TURSO_SCHEMA_CHECKED", False)

    def reject_network(*_args, **_kwargs):
        raise AssertionError("Live network access is disabled in tests; mock the transport")

    def reject_dns(*_args, **_kwargs):
        raise socket.gaierror("DNS is disabled in offline tests")

    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair
    local_pair = threading.local()

    def offline_connect(sock, address):
        # Windows asyncio builds its internal wakeup pipe using a loopback pair.
        if getattr(local_pair, "building", False) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        return reject_network()

    def offline_socketpair(*args, **kwargs):
        local_pair.building = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            local_pair.building = False

    monkeypatch.setattr(requests.sessions.Session, "request", reject_network)
    monkeypatch.setattr(socket.socket, "connect", offline_connect)
    monkeypatch.setattr(socket, "socketpair", offline_socketpair)
    monkeypatch.setattr(socket, "getaddrinfo", reject_dns)
