"""Regression: Compose project and container names must be explicit."""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_compose_project_and_container_name_explicit():
    text = (ROOT / "deploy" / "docker-compose.yml").read_text()
    assert text.startswith("name: wuwaterm-telegram\n")
    assert "container_name: wuwaterm-telegram" in text
