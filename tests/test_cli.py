import pytest

from clisweave.cli import UsageError, build_command


def test_claude_all_flags():
    cmd = build_command("claude", ["-p", "-c", "-m", "sonnet", "--add-dir", "/tmp", "-y", "hello world"])
    assert cmd == [
        "claude", "-p", "--continue", "--model", "sonnet",
        "--add-dir", "/tmp", "--dangerously-skip-permissions", "hello world",
    ]


def test_codex_print_and_continue_uses_exec_resume_last():
    cmd = build_command("codex", ["-p", "-c"])
    assert cmd == ["codex", "exec", "resume", "--last"]


def test_codex_interactive_ignores_continue():
    # codex has no non-interactive "continue"; -c without -p is a no-op,
    # matching what the underlying `codex` CLI supports.
    cmd = build_command("codex", ["-c"])
    assert cmd == ["codex"]


def test_codex_yolo_maps_to_approve_for_me():
    cmd = build_command("codex", ["-y"])
    assert cmd == ["codex", "--approve-for-me"]


def test_kimi_all_flags():
    cmd = build_command("kimi", ["-p", "-c", "-m", "kimi-for-coding", "-y"])
    assert cmd == ["kimi", "-p", "-c", "-m", "kimi-for-coding", "-y"]


def test_trae_all_flags():
    cmd = build_command("trae", ["-p", "-c", "-m", "sonnet", "--add-dir", "/tmp", "hello world"])
    assert cmd == ["trae", "-p", "-c", "-m", "sonnet", "--add-dir", "/tmp", "hello world"]


def test_trae_yolo_is_accepted_but_not_translated():
    # Deliberate no-op, not an oversight: Trae is an IDE whose auto-approve
    # lives in its own settings rather than behind a CLI flag (see the
    # per-tool --yolo mapping in cli.USAGE). -y is still accepted so scripts
    # and aliases written against another tool keep working across all four,
    # but it must not be forwarded through as-is.
    cmd = build_command("trae", ["-p", "-y"])
    assert cmd == ["trae", "-p"]

    cmd = build_command("trae", ["--yolo"])
    assert cmd == ["trae"]


def test_multiple_add_dirs_repeat_flag():
    cmd = build_command("claude", ["--add-dir", "/a", "--add-dir", "/b"])
    assert cmd == ["claude", "--add-dir", "/a", "--add-dir", "/b"]


def test_double_dash_passes_rest_through_untouched():
    cmd = build_command("claude", ["--", "--agent", "reviewer", "-p"])
    assert cmd == ["claude", "--agent", "reviewer", "-p"]


def test_unrecognized_flag_passes_through():
    cmd = build_command("kimi", ["--plan", "do the thing"])
    assert cmd == ["kimi", "--plan", "do the thing"]


def test_unknown_tool_raises_usage_error():
    with pytest.raises(UsageError, match="unknown tool"):
        build_command("bogus", [])


@pytest.mark.parametrize("flag", ["-m", "--model", "--add-dir"])
def test_flag_missing_value_raises_usage_error(flag):
    with pytest.raises(UsageError, match="requires a value"):
        build_command("claude", [flag])
