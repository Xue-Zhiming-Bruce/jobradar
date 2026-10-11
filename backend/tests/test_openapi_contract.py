"""The committed contract must be what the app generates right now.

Nothing here writes to the working tree — the document is compared in memory —
so `git status --short` is still empty after the suite has run.
"""

import json
import socket
from pathlib import Path

from app.export_openapi import CONTRACT_PATH, render_contract
from app.main import app


def test_committed_contract_matches_the_app() -> None:
    """Hand-editing openapi.json — a 200 turned into a 201, say — fails here."""
    committed = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert committed == app.openapi()


def test_committed_contract_is_the_rendered_contract_byte_for_byte() -> None:
    assert CONTRACT_PATH.read_bytes() == render_contract(app).encode("utf-8")


def test_contract_describes_only_the_health_check() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert {"openapi", "info", "paths"} <= set(contract)
    assert list(contract["paths"]) == ["/healthz"]

    ok_response = contract["paths"]["/healthz"]["get"]["responses"]["200"]
    assert ok_response["content"]["application/json"]["schema"]


def test_contract_carries_no_environment_specific_values() -> None:
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    contract = json.loads(text)

    assert "servers" not in contract
    assert str(Path.home()) not in text
    assert "/Users/" not in text
    assert socket.gethostname() not in text

    lowered = text.lower()
    for secret_like in ("secret", "token", "api_key", "apikey", "password", "database_url"):
        assert secret_like not in lowered
