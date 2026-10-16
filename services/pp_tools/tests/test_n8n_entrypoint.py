"""n8n/entrypoint.sh must parse and run: a stray apostrophe once stopped n8n from starting (F-015)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
ENTRYPOINT = REPO / "n8n" / "entrypoint.sh"


def test_entrypoint_is_valid_shell():
    assert subprocess.run(["sh", "-n", str(ENTRYPOINT)], capture_output=True).returncode == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_entrypoint_writes_all_credentials(tmp_path):
    out = tmp_path / "creds.json"
    fake = tmp_path / "n8n"
    fake.write_text('#!/bin/sh\nfor a in "$@"; do case "$a" in --input=*) cp "${a#--input=}" "' + str(out) + '";; esac; done\n')
    fake.chmod(0o755)
    env = {**os.environ, "GROQ_API_KEY": "gsk_test", "N8N_BIN": str(fake), "PP_IMPORT_WORKFLOWS_ON_START": "false"}
    run = subprocess.run(["sh", str(ENTRYPOINT)], env=env, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    creds = {c["id"]: c for c in json.loads(out.read_text())}
    assert creds["ppGroqCredent001"]["data"]["apiKey"] == "gsk_test"
    assert creds["ppGroqHeader0001"]["type"] == "httpHeaderAuth"
    assert creds["ppGroqHeader0001"]["data"] == {"name": "Authorization", "value": "Bearer gsk_test"}
