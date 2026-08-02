"""Test isolation from the developer's own environment.

`Settings` reads `.env`/`../.env` by design -- that's how the app is
configured. It also meant the test suite silently inherited whatever the
machine happened to be running: pointing `.env` at the SQLite backend and a
remote embedding server made tests construct a real store and make real
network calls, and the suite went from 1.7s to 27s and started failing on
config it never chose.

Tests must describe their own world. This clears both sources of ambient
config -- the dotenv file and the process environment -- so a `Settings()`
built in a test has defaults plus exactly what that test passed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sellowl.config import Settings

# Every field that has a real-world value in a working .env. Anything a test
# actually needs, it sets explicitly.
_LEAKY_ENV_VARS = tuple(f.upper() for f in Settings.model_fields)


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in _LEAKY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Clearing env vars is not enough for these two: the *code* defaults point
    # at real resources, so tests inherit them by doing nothing wrong.
    #
    # An embeddings endpoint tests would dial and wait out the retries of
    # (2s -> 86s suite), and the live comp store, which tests both read (a
    # 4000-comp cosine scan per call) and *write* -- a fixture comp was found
    # sitting in the real corpus. A test must not be able to corrupt the data
    # the app serves.
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "comps.db"))
