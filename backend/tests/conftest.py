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

import pytest

from sellowl.config import Settings

# Every field that has a real-world value in a working .env. Anything a test
# actually needs, it sets explicitly.
_LEAKY_ENV_VARS = tuple(f.upper() for f in Settings.model_fields)


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in _LEAKY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Clearing env vars is not enough for this one: the *code* default points
    # at a real embeddings endpoint, so tests would try to reach it (and wait
    # out its retries -- this took the suite from 2s to 86s). Tests declare
    # "no embedding server" and get the built-in lexical embedder.
    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
