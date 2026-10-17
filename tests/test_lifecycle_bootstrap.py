"""初始化必须迁移独立回执库；任何迁移失败都不能继续进入应用准备。"""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("accounting_fails", [False, True])
def test_bootstrap_migrates_both_databases_and_stops_on_accounting_failure(tmp_path, accounting_fails):
    log = tmp_path / "calls"
    stub = (
        '#!/bin/sh\n'
        'printf "%s|%s|%s\\n" "${0##*/}" "$*" "$MODEL_CALLS_ENABLED" >> "$BOOTSTRAP_LOG"\n'
        'if [ "${FAIL_ACCOUNTING:-false}" = true ] && [ "${1:-}" = scripts/migrate_accounting.py ]; then exit 19; fi\n'
    )
    for name in ("alembic", "python"):
        executable = tmp_path / name
        executable.write_text(stub)
        executable.chmod(0o700)
    result = subprocess.run(
        ["/bin/sh", str(Path(__file__).resolve().parents[1] / "scripts/bootstrap.sh"), "production"],
        env={**os.environ, "PATH": str(tmp_path), "BOOTSTRAP_LOG": str(log),
             "FAIL_ACCOUNTING": str(accounting_fails).lower(), "MODEL_CALLS_ENABLED": "true"},
        capture_output=True, text=True, timeout=10,
    )
    calls = log.read_text().splitlines()
    assert calls[:2] == ["alembic|upgrade head|false", "python|scripts/migrate_accounting.py --lifecycle|false"]
    if accounting_fails:
        assert result.returncode == 19 and len(calls) == 2
    else:
        assert result.returncode == 0, result.stderr
        assert calls[2].startswith("python|scripts/migrate_workspace.py ")
