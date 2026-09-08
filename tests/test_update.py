import pytest

from clisweave import update


def test_detect_repo_dir_finds_git_root(tmp_path):
    repo = tmp_path / "clisweave"
    (repo / ".git").mkdir(parents=True)
    package_dir = repo / "src" / "clisweave"
    package_dir.mkdir(parents=True)

    assert update.detect_repo_dir(str(package_dir)) == str(repo)


def test_detect_repo_dir_none_for_pip_install(tmp_path):
    # No .git two levels up -- looks like a site-packages install.
    package_dir = tmp_path / "site-packages" / "clisweave"
    package_dir.mkdir(parents=True)

    assert update.detect_repo_dir(str(package_dir)) is None


def test_cmd_update_git_install_runs_git_pull(monkeypatch, capsys):
    monkeypatch.setattr(update, "detect_repo_dir", lambda _pkg: "/some/repo")

    calls = []

    class FakeResult:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return FakeResult()

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        update.cmd_update([])

    assert exc_info.value.code == 0
    assert calls == [["git", "-C", "/some/repo", "pull", "--ff-only"]]
    assert "Updating git install" in capsys.readouterr().out


def test_cmd_update_pip_install_runs_pip_upgrade(monkeypatch, capsys):
    monkeypatch.setattr(update, "detect_repo_dir", lambda _pkg: None)

    calls = []

    class FakeResult:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return FakeResult()

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    monkeypatch.setattr(update.sys, "executable", "/fake/python")

    with pytest.raises(SystemExit) as exc_info:
        update.cmd_update([])

    assert exc_info.value.code == 0
    assert calls == [["/fake/python", "-m", "pip", "install", "--upgrade", "clisweave"]]
    assert "Updating pip install" in capsys.readouterr().out


def test_cmd_update_propagates_nonzero_exit(monkeypatch):
    monkeypatch.setattr(update, "detect_repo_dir", lambda _pkg: "/some/repo")

    class FakeResult:
        returncode = 1

    monkeypatch.setattr(update.subprocess, "run", lambda *a, **kw: FakeResult())

    with pytest.raises(SystemExit) as exc_info:
        update.cmd_update([])
    assert exc_info.value.code == 1


def test_cmd_update_rejects_extra_arguments(capsys):
    with pytest.raises(SystemExit) as exc_info:
        update.cmd_update(["--bogus"])
    assert exc_info.value.code == 1
    assert "unexpected argument" in capsys.readouterr().err


# ---------- update_tools ----------

def test_update_tools_skips_missing_binaries(monkeypatch, capsys):
    monkeypatch.setattr(update.shutil, "which", lambda _tool: None)
    calls = []
    monkeypatch.setattr(update.subprocess, "run", lambda argv, **kw: calls.append(argv))

    assert update.update_tools() == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "claude: not installed, skipping" in out
    assert "codex: not installed, skipping" in out
    assert "kimi: not installed, skipping" in out


def test_update_tools_runs_each_installed_tools_update_command(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    calls = []

    class FakeResult:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return FakeResult()

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_tools() == 0
    assert calls == [["claude", "update"], ["codex", "update"], ["kimi", "update"]]


def test_update_tools_prints_hint_when_claude_fails(monkeypatch, capsys):
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    class FakeResult:
        def __init__(self, code):
            self.returncode = code

    codes = {"claude": 1, "codex": 0, "kimi": 0}
    monkeypatch.setattr(update.subprocess, "run", lambda argv, **kw: FakeResult(codes[argv[0]]))

    update.update_tools()

    err = capsys.readouterr().err
    assert "hint:" in err
    assert "downloads.claude.ai" in err


def test_update_tools_retries_claude_and_succeeds_without_hint(monkeypatch, capsys):
    """Regression test: claude's own updater has no fallback mirror and can
    hit its internal download deadline on a slow-but-working connection --
    confirmed live, a failed claude update succeeded on a bare retry with
    no other change. update_tools should retry claude automatically rather
    than giving up (and printing the hint) after a single failure."""
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    class FakeResult:
        def __init__(self, code):
            self.returncode = code

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv[0])
        if argv[0] == "claude" and calls.count("claude") == 1:
            return FakeResult(1)  # fails first attempt
        return FakeResult(0)  # succeeds on retry (and codex/kimi succeed normally)

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_tools() == 0
    assert calls.count("claude") == 2
    assert "hint:" not in capsys.readouterr().err


def test_update_tools_gives_up_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    class FakeResult:
        returncode = 1

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv[0])
        return FakeResult()

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_tools() != 0
    # 1 initial attempt + TOOL_UPDATE_RETRIES["claude"] retries, then give up
    assert calls.count("claude") == 1 + update.TOOL_UPDATE_RETRIES["claude"]


def test_update_tools_no_hint_when_codex_or_kimi_fail(monkeypatch, capsys):
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    class FakeResult:
        def __init__(self, code):
            self.returncode = code

    codes = {"claude": 0, "codex": 1, "kimi": 1}
    monkeypatch.setattr(update.subprocess, "run", lambda argv, **kw: FakeResult(codes[argv[0]]))

    update.update_tools()

    assert "hint:" not in capsys.readouterr().err


def test_update_tools_reports_worst_exit_code_but_keeps_going(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    class FakeResult:
        def __init__(self, code):
            self.returncode = code

    codes = {"claude": 0, "codex": 3, "kimi": 0}
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return FakeResult(codes[argv[0]])

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_tools() == 3
    # all three still ran despite codex failing
    assert len(calls) == 3


# ---------- cmd_update dispatch modes ----------

def test_cmd_update_tools_mode_only_updates_tools(monkeypatch):
    self_calls = []
    tools_calls = []
    monkeypatch.setattr(update, "update_self", lambda: self_calls.append(1) or 0)
    monkeypatch.setattr(update, "update_tools", lambda: tools_calls.append(1) or 0)

    with pytest.raises(SystemExit) as exc_info:
        update.cmd_update(["tools"])

    assert exc_info.value.code == 0
    assert self_calls == []
    assert tools_calls == [1]


def test_cmd_update_all_mode_updates_both(monkeypatch):
    self_calls = []
    tools_calls = []
    monkeypatch.setattr(update, "update_self", lambda: self_calls.append(1) or 0)
    monkeypatch.setattr(update, "update_tools", lambda: tools_calls.append(1) or 0)

    with pytest.raises(SystemExit) as exc_info:
        update.cmd_update(["all"])

    assert exc_info.value.code == 0
    assert self_calls == [1]
    assert tools_calls == [1]


def test_cmd_update_bare_mode_only_updates_self(monkeypatch):
    self_calls = []
    tools_calls = []
    monkeypatch.setattr(update, "update_self", lambda: self_calls.append(1) or 0)
    monkeypatch.setattr(update, "update_tools", lambda: tools_calls.append(1) or 0)

    with pytest.raises(SystemExit) as exc_info:
        update.cmd_update([])

    assert exc_info.value.code == 0
    assert self_calls == [1]
    assert tools_calls == []


def test_cmd_update_rejects_unknown_target(capsys):
    with pytest.raises(SystemExit) as exc_info:
        update.cmd_update(["bogus"])
    assert exc_info.value.code == 1
    assert "unexpected argument" in capsys.readouterr().err
