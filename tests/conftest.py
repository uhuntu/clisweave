"""Shared pytest setup.

`trae_light_records()` scans the real Trae IDE install directories under
%APPDATA% -- a global, machine-wide location with no per-test override, unlike
claude/codex/kimi whose roots (CLAUDE_PROJECTS, CODEX_HOME, KIMI_HOME) tests
repoint at a tmp_path. Left alone, every test reaching cmd_list, cmd_stats, or
gather_candidates picks up whatever Trae sessions exist on the developer's
machine, so those assertions silently depend on local state.

No Trae directory is visible by default: tests that exercise Trae repoint
`_trae_base_dirs` at their own tmp_path instead.
"""
import pytest

from clisweave import sessions


@pytest.fixture(autouse=True)
def no_real_trae_dirs(monkeypatch):
    monkeypatch.setattr(sessions, "_trae_base_dirs", lambda: [])
