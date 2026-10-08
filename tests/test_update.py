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


def test_update_self_retries_a_failed_git_pull(monkeypatch):
    """The pull is small, but this network drops TLS connections at random:
    one that died on SSL_ERROR_SYSCALL succeeded on the very next try
    (live 2026-10-07). The self-update used to get a single attempt."""
    monkeypatch.setattr(update, "detect_repo_dir", lambda _pkg: "/some/repo")
    monkeypatch.delenv(update.UPDATE_PROXY_ENV, raising=False)

    class FakeResult:
        def __init__(self, code):
            self.returncode = code

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return FakeResult(1 if len(calls) == 1 else 0)  # fails once, then succeeds

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_self() == 0
    assert len(calls) == 2


def test_update_self_retry_goes_via_proxy(monkeypatch):
    monkeypatch.setattr(update, "detect_repo_dir", lambda _pkg: "/some/repo")
    monkeypatch.setenv(update.UPDATE_PROXY_ENV, "http://127.0.0.1:7897")
    monkeypatch.setenv("no_proxy", "example.invalid")

    class FakeResult:
        def __init__(self, code):
            self.returncode = code

    envs = []

    def fake_run(argv, **kwargs):
        envs.append(kwargs.get("env"))
        return FakeResult(1 if len(envs) == 1 else 0)

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_self() == 0
    assert envs[0] is None  # first attempt stays direct
    assert envs[1]["http_proxy"] == "http://127.0.0.1:7897"
    assert envs[1]["https_proxy"] == "http://127.0.0.1:7897"
    assert "no_proxy" not in envs[1]


def test_update_self_gives_up_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(update, "detect_repo_dir", lambda _pkg: "/some/repo")
    monkeypatch.delenv(update.UPDATE_PROXY_ENV, raising=False)

    class FakeResult:
        returncode = 1

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return FakeResult()

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_self() != 0
    assert len(calls) == 1 + update.SELF_UPDATE_RETRIES


def test_update_self_does_not_retry_a_failure_a_retry_cannot_fix(monkeypatch, capsys):
    """A dirty worktree fails identically on every attempt: retrying only
    repeats the error, and a network/proxy hint would point at the wrong
    problem entirely."""
    monkeypatch.setattr(update, "detect_repo_dir", lambda _pkg: "/some/repo")
    monkeypatch.delenv(update.UPDATE_PROXY_ENV, raising=False)

    class FakeResult:
        returncode = 1
        stdout = ("error: cannot pull with rebase: You have unstaged changes.\n"
                  "error: Please commit or stash them.\n")

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return FakeResult()

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_self() != 0
    assert len(calls) == 1  # no retries
    err = capsys.readouterr().err
    assert "not retried" in err
    assert update.UPDATE_PROXY_ENV not in err  # not a network problem


def test_update_self_hints_at_the_proxy_when_it_keeps_failing(monkeypatch, capsys):
    monkeypatch.setattr(update, "detect_repo_dir", lambda _pkg: "/some/repo")
    monkeypatch.delenv(update.UPDATE_PROXY_ENV, raising=False)

    class FakeResult:
        returncode = 1

    monkeypatch.setattr(update.subprocess, "run", lambda *a, **kw: FakeResult())

    assert update.update_self() != 0
    err = capsys.readouterr().err
    assert "hint:" in err
    assert update.UPDATE_PROXY_ENV in err


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
    assert "step: not installed, skipping" in out


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
    assert calls == [["claude", "update"], ["codex", "update"],
                     ["kimi", "update", "--yes"], ["step", "update"]]


def test_update_tools_prints_hint_when_claude_fails(monkeypatch, capsys):
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    class FakeResult:
        def __init__(self, code):
            self.returncode = code

    codes = {"claude": 1, "codex": 0, "kimi": 0}
    monkeypatch.setattr(update.subprocess, "run",
                        lambda argv, **kw: FakeResult(codes.get(argv[0], 0)))

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
    monkeypatch.delenv(update.UPDATE_PROXY_ENV, raising=False)

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


def test_update_tools_retries_kimi(monkeypatch):
    """kimi's update check intermittently hangs at connect time; a bare retry
    usually gets through, same failure class as claude."""
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    class FakeResult:
        def __init__(self, code):
            self.returncode = code

    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv[0])
        if argv[0] == "kimi" and seen.count("kimi") == 1:
            return FakeResult(1)  # failed check, then succeeds on retry
        return FakeResult(0)

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_tools() == 0
    # failed once, then succeeded on the first retry (didn't need all of them)
    assert seen.count("kimi") == 2


def test_update_tools_first_attempt_is_direct_retry_goes_via_proxy(monkeypatch):
    """With CLISWEAVE_UPDATE_PROXY set, a failed codex update retries through
    the proxy (and succeeds): this network severs codex's ~146MB direct
    transfer at ~60MB every time, while the proxy path completes in a minute."""
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setenv(update.UPDATE_PROXY_ENV, "http://127.0.0.1:7897")
    monkeypatch.setenv("no_proxy", "example.invalid")

    seen = []

    class FakeResult:
        def __init__(self, code):
            self.returncode = code

    def fake_run(argv, **kwargs):
        seen.append((argv[0], kwargs.get("env")))
        if argv[0] == "codex" and len([s for s in seen if s[0] == "codex"]) == 1:
            return FakeResult(1)  # direct attempt fails...
        return FakeResult(0)  # ...proxy retry succeeds

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_tools() == 0

    codex_attempts = [env for tool, env in seen if tool == "codex"]
    assert len(codex_attempts) == 2
    # first attempt: direct (env=None means inherit-as-is)
    assert codex_attempts[0] is None
    # retry: forced through the proxy, no_proxy exemptions stripped
    retry_env = codex_attempts[1]
    assert retry_env["http_proxy"] == "http://127.0.0.1:7897"
    assert retry_env["https_proxy"] == "http://127.0.0.1:7897"
    assert "no_proxy" not in retry_env


def test_update_tools_retry_stays_direct_when_no_proxy_configured(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.delenv(update.UPDATE_PROXY_ENV, raising=False)

    class FakeResult:
        returncode = 0

    envs = []

    def fake_run(argv, **kwargs):
        envs.append(kwargs.get("env"))
        return FakeResult()

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_tools() == 0
    assert envs and all(env is None for env in envs)


def test_update_tools_no_hint_when_kimi_fails(monkeypatch, capsys):
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    class FakeResult:
        def __init__(self, code):
            self.returncode = code

    codes = {"claude": 0, "codex": 0, "kimi": 1}
    monkeypatch.setattr(update.subprocess, "run",
                        lambda argv, **kw: FakeResult(codes.get(argv[0], 0)))

    update.update_tools()

    assert "hint:" not in capsys.readouterr().err


def test_update_tools_prints_proxy_hint_when_codex_fails(monkeypatch, capsys):
    """codex's ~146MB asset dies at ~60MB on networks that sever long transfers;
    the failure hint should point at the proxy escape hatch."""
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.delenv(update.UPDATE_PROXY_ENV, raising=False)

    class FakeResult:
        def __init__(self, code):
            self.returncode = code

    codes = {"claude": 0, "codex": 1, "kimi": 0}
    monkeypatch.setattr(update.subprocess, "run",
                        lambda argv, **kw: FakeResult(codes.get(argv[0], 0)))

    update.update_tools()

    err = capsys.readouterr().err
    assert "hint:" in err
    assert update.UPDATE_PROXY_ENV in err


def test_update_tools_detects_codex_curl_failure_hidden_by_zero_exit(monkeypatch, capsys):
    """Codex 0.160.1 pipes curl into sh without pipefail, then prints a
    success banner even when curl's TLS connection failed."""
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.delenv(update.UPDATE_PROXY_ENV, raising=False)

    class FakeResult:
        returncode = 0
        stdout = ""

    def fake_run(argv, **kwargs):
        result = FakeResult()
        if argv[0] == "codex":
            result.stdout = (
                "curl: (35) TLS connect error: unexpected eof while reading\n"
                "🎉 Update ran successfully! Please restart Codex.\n"
            )
        return result

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_tools() != 0
    captured = capsys.readouterr()
    assert "reported success after its installer failed" in captured.err
    assert update.UPDATE_PROXY_ENV in captured.err
    assert "curl: (35)" in captured.out


def test_update_tools_ignores_codex_curl_failure_when_the_install_recovered(
    monkeypatch, capsys
):
    """A curl diagnostic against the primary host is not a failed update when
    install.sh falls back to GitHub Releases and installs anyway.  Seen live
    2026-10-08: `curl: (28) SSL connection timeout`, then the fallback warning,
    then a real install of 0.161.0, exit 0 -- `cw update` must not fail codex
    for that, nor print the proxy hint for a network the fallback routed around.

    Note what is absent from this fixture: install.sh never ran, so there is no
    "==> Updating Codex CLI from" line -- which is what separates this from the
    recovered case, since codex's own success banner appears in both."""
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.delenv(update.UPDATE_PROXY_ENV, raising=False)

    class FakeResult:
        returncode = 0
        stdout = ""

    def fake_run(argv, **kwargs):
        result = FakeResult()
        if argv[0] == "codex":
            result.stdout = (
                "curl: (28) SSL connection timeout\n"
                "WARNING: releases.openai.com is unavailable; falling back to "
                "GitHub Releases.\n"
                "==> Updating Codex CLI from 0.160.1 to 0.161.0\n"
                "==> Detected platform: Linux (x64)\n"
                "==> Codex CLI 0.161.0 installed successfully.\n"
                "\n"
                "🎉 Update ran successfully! Please restart Codex.\n"
            )
        return result

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_tools() == 0
    captured = capsys.readouterr()
    assert "reported success after its installer failed" not in captured.err
    assert "hint:" not in captured.err
    # the updater's own output is still shown, curl diagnostic included
    assert "curl: (28)" in captured.out


def test_update_tools_reports_worst_exit_code_but_keeps_going(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    # An ambient proxy must not change the call count: the proxy retry is a
    # fallback that only exists when UPDATE_PROXY_ENV is configured, and the
    # first attempt is always direct.
    monkeypatch.delenv(update.UPDATE_PROXY_ENV, raising=False)

    class FakeResult:
        def __init__(self, code):
            self.returncode = code

    codes = {"claude": 0, "codex": 3, "kimi": 0, "step": 0}
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return FakeResult(codes.get(argv[0], 0))

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    assert update.update_tools() == 3
    # every installed tool still ran despite codex failing
    assert len(calls) == 4


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
