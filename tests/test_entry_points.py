"""Entry points that misbehave rather than fail.

Each of these produced a wrong answer or a raw traceback on a path that has
a perfectly good clean-error path next to it -- a Windows npm shim that
`shutil.which` finds but CreateProcess cannot spawn (which aborted
`ai update tools` before the remaining tools were tried), a `--cwd` that was
validated on one entry point and silently ignored on another, a non-atomic
cache write, and a superscript "digit" reaching int().
"""

import json
import os
import time

import pytest
from clisweave import sessions, update


# ---------- ai update ----------

def test_detect_repo_dir_accepts_a_dot_git_file(tmp_path):
    """A worktree, a submodule and a `--separate-git-dir` clone all carry
    .git as a file. Treating only a directory as a repo sent `ai update`
    down the pip branch -- quietly updating (or downgrading) the PyPI
    release instead of the clone the `ai` launcher actually points at."""
    # an editable/curl install: the package sits in the clone's src/
    pkg = tmp_path / "clone" / "src" / "clisweave"
    pkg.mkdir(parents=True)
    (tmp_path / "clone" / ".git").write_text("gitdir: /elsewhere/clisweave/.git/worktrees/wt\n")

    assert update.detect_repo_dir(str(pkg)) == str(tmp_path / "clone")


def test_run_update_command_reports_an_unspawnable_tool_cleanly(monkeypatch, capsys):
    """A spawn failure used to escape update_tools entirely, so codex and
    kimi were never attempted and the user got a traceback."""
    def cannot_spawn(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(update.subprocess, "run", cannot_spawn)

    code, _output = update.run_update_command(["kimi", "update", "--yes"])
    assert code == 127
    assert "could not run kimi" in capsys.readouterr().err


def test_update_tools_continues_past_a_tool_that_cannot_be_spawned(monkeypatch, capsys):
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    def cannot_spawn_claude(argv, **kwargs):
        if argv[0] == "claude":
            raise FileNotFoundError(2, "No such file or directory", "claude")

        class Ok:
            returncode = 0
        return Ok()

    monkeypatch.setattr(update.subprocess, "run", cannot_spawn_claude)

    assert update.update_tools() == 127
    out = capsys.readouterr().err
    # kimi was still attempted rather than skipped by an uncaught exception
    assert "kimi: not" not in out


def test_run_update_command_routes_a_windows_shim_through_the_shell(monkeypatch):
    """A Windows npm-global shim (claude.cmd) passes shutil.which but
    CreateProcess only appends .exe, so it cannot be spawned directly."""
    commands = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(update.shutil, "which",
                        lambda tool: f"C:\\tools\\{tool}.CMD")
    monkeypatch.setattr(update.subprocess, "run",
                        lambda command, **kw: commands.append(command) or type("R", (), {"returncode": 0})())

    update.run_update_command(["claude", "update"])

    assert commands[0][:2] == [os.environ.get("COMSPEC", "cmd.exe"), "/c"]
    assert commands[0][2:] == ["claude", "update"]


# ---------- --cwd validation ----------

def test_handoff_by_number_rejects_a_cwd_that_is_not_a_directory(monkeypatch, tmp_path, capsys):
    """`ai 3 codex --cwd /typo` exported the transcript and started codex in
    the current directory with no warning, while
    `ai resume claude <id> --cwd /typo` rejected it cleanly."""
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    sessions.write_list_cache([{"tool": "claude", "id": "abc"}])
    monkeypatch.setattr(sessions, "perform_handoff",
                        lambda *a, **kw: pytest.fail("handoff should not run"))

    with pytest.raises(SystemExit):
        sessions.cmd_resume(["3", "codex", "--cwd", str(tmp_path / "typo")])

    assert "is not a directory" in capsys.readouterr().err


def test_cwd_override_accepts_a_real_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    assert sessions.extract_cwd_override(["--cwd", str(tmp_path), "3"]) == (["3"], str(tmp_path))


# ---------- the listing cache ----------

def test_write_list_cache_replaces_rather_than_truncates(monkeypatch, tmp_path):
    cache = tmp_path / "last_list.json"
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(cache))

    sessions.write_list_cache([{"tool": "claude", "id": "first"}])
    first_generation = cache.read_text()

    sessions.write_list_cache([{"tool": "codex", "id": "second"}])

    assert cache.read_text() == json.dumps([{"tool": "codex", "id": "second"}])
    assert first_generation  # ...and there was a previous one
    # no temp file left behind
    assert [p.name for p in tmp_path.iterdir()] == ["last_list.json"]


def test_write_list_cache_survives_an_unwritable_location(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "no" / "such" / "dir" / "c.json"))
    sessions.write_list_cache([{"tool": "claude", "id": "abc"}])  # must not raise


# ---------- handoff exports ----------

def _write_export(handoff_dir, name, age_days):
    path = handoff_dir / name
    path.write_text("transcript")
    when = time.time() - age_days * 86400
    os.utime(path, (when, when))
    return path


def test_handoff_exports_are_pruned_by_age(monkeypatch, tmp_path):
    handoff_dir = tmp_path / "handoffs"
    handoff_dir.mkdir()
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(handoff_dir))

    old = _write_export(handoff_dir, "claude-old.md", age_days=30)
    current = _write_export(handoff_dir, "claude-current.md", age_days=0)

    sessions.prune_handoff_exports()

    assert not old.exists()
    assert current.exists()


def test_age_wins_over_the_keep_count(monkeypatch, tmp_path):
    """Twenty old exports are not kept just because there are fewer than
    HANDOFF_KEEP of them -- age is the first cut."""
    handoff_dir = tmp_path / "handoffs"
    handoff_dir.mkdir()
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(handoff_dir))

    for n in range(5):
        _write_export(handoff_dir, f"ancient-{n}.md", age_days=30)

    sessions.prune_handoff_exports()

    assert list(handoff_dir.iterdir()) == []


def test_handoff_exports_are_pruned_by_count(monkeypatch, tmp_path):
    """Nothing deleted these, and a transcript is as long as the conversation
    was, so the directory grew one file per handoff forever."""
    handoff_dir = tmp_path / "handoffs"
    handoff_dir.mkdir()
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(handoff_dir))
    monkeypatch.setattr(sessions, "HANDOFF_KEEP", 3)

    for n in range(10):
        _write_export(handoff_dir, f"export-{n}.md", age_days=0)

    sessions.prune_handoff_exports()

    # the three newest survive, by mtime, not by name
    assert sorted(p.name for p in handoff_dir.iterdir()) == [
        f"export-{n}.md" for n in (7, 8, 9)]


def test_prune_survives_a_missing_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(tmp_path / "no-handoffs"))
    sessions.prune_handoff_exports()  # must not raise


def test_prune_survives_a_file_that_vanishes_mid_sweep(monkeypatch, tmp_path):
    handoff_dir = tmp_path / "handoffs"
    handoff_dir.mkdir()
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(handoff_dir))
    _write_export(handoff_dir, "a.md", age_days=30)
    _write_export(handoff_dir, "b.md", age_days=30)

    real_remove = os.remove
    monkeypatch.setattr(os, "remove",
                        lambda path: real_remove(path) if path.endswith("a.md")
                        else (_ for _ in ()).throw(FileNotFoundError(path)))

    sessions.prune_handoff_exports()  # must not raise


# ---------- row numbers ----------
@pytest.mark.parametrize("arg", ["1", "42"])
def test_a_row_number_is_an_ascii_digit_string(arg):
    assert sessions.ROW_NUMBER_RE.match(arg)


@pytest.mark.parametrize("arg", ["²", "½", "3x", "x", "-3"])
def test_a_superscript_or_other_numeric_character_is_not_a_row_number(arg):
    """'²'.isdigit() is True but int('²') raises, so `ai ²` used to end in a
    ValueError traceback."""
    assert not sessions.ROW_NUMBER_RE.match(arg)
