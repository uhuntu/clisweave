"""The listing you see every time you type `ai`.

It used to be a plain table where every row weighed the same: no color, no
marker for the directory you are standing in, a WHEN column that read like a
stopwatch, and len()-measured columns that a Chinese title pushed out of
line. These cover the reworked renderer -- the color gates, the turn counts
each tool's store really supports, the current-directory marker, and the
encoding fallbacks that keep a GBK console readable.

Fixtures are written with compact separators because that is what the CLIs
themselves write, and the turn counter matches those bytes as substrings.
"""

import io
import json
import os
import re
import sys

import pytest

from crossweave import codex, color, common, sessions


# ---------- capturing what render_rows prints ----------

def _capture(monkeypatch, encoding="utf-8"):
    """A stdout stand-in with a known encoding: render_rows prints to it,
    the test reads its bytes back."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding=encoding)
    monkeypatch.setattr(sys, "stdout", stream)
    return stream


def _printed(stream):
    stream.flush()
    return stream.buffer.getvalue().decode(stream.encoding, "replace")


def _plain(monkeypatch):
    """render_rows with color off -- the shape the width tests assert on."""
    monkeypatch.setattr(color, "_STATE", False)


# ---------- fixture transcripts, one per tool ----------

def _dump(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in records) + "\n",
                    encoding="utf-8")
    return path


def write_claude(tmp_path, sid, records):
    return _dump(tmp_path / ".claude" / "projects" / "--work" / f"{sid}.jsonl", records)


def claude_user(text):
    return {"type": "user", "message": {"role": "user",
                                        "content": [{"type": "text", "text": text}]}}


def claude_tool_result():
    # claude stores a tool result as a user record too -- counting records
    # would call every tool call a turn.
    return {"type": "user", "message": {"role": "user",
                                        "content": [{"type": "tool_result", "content": "ok"}]}}


def write_step(tmp_path, sid, records):
    return _dump(tmp_path / ".stepcode" / "agent" / "sessions" / "--work" / f"{sid}.jsonl", records)


def step_user(text):
    return {"type": "message", "message": {"role": "user", "content": text}}


def write_codex(tmp_path, sid, records):
    day = tmp_path / ".codex" / "sessions" / "2026" / "01" / "02"
    day.mkdir(parents=True, exist_ok=True)
    return _dump(day / f"rollout-2026-01-02T00-00-00-{sid}.jsonl", records)


def codex_user(text):
    return {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                 "content": [{"type": "input_text", "text": text}]}}


def codex_tool_call():
    # a function_call carries no role at all
    return {"type": "response_item", "payload": {"type": "function_call",
                                                 "name": "shell", "arguments": "{}"}}


def write_kimi(tmp_path, sid, records):
    return _dump(tmp_path / ".kimi-code" / sid / "agents" / "main" / "wire.jsonl", records)


def kimi_prompt(text):
    return {"type": "turn.prompt", "input": [{"type": "text", "text": text}]}


def kimi_reply(text):
    return {"type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {"type": "text", "text": text}}}


def turns(tool, path=None, dir_=None):
    record = {"tool": tool}
    if path is not None:
        record["path"] = str(path)
    if dir_ is not None:
        record["dir"] = str(dir_)
    return sessions.session_turns(record)


# ---------- turns ----------

def test_turns_count_the_user_messages_each_tool_records(tmp_path):
    claude = write_claude(tmp_path, "c1", [claude_user("one"), claude_user("two"),
                                           {"type": "assistant", "message": {"role": "assistant",
                                                                            "content": []}}])
    step = write_step(tmp_path, "s1", [{"type": "session", "id": "s1"},
                                       step_user("one"), step_user("two"),
                                       {"type": "message", "message": {"role": "assistant",
                                                                       "content": "done"}}])
    codex = write_codex(tmp_path, "x1", [codex_user("one"), codex_tool_call(), codex_user("two")])
    write_kimi(tmp_path, "k1", [kimi_prompt("one"), kimi_reply("hi"), kimi_prompt("two")])
    kimi_dir = tmp_path / ".kimi-code" / "k1"

    assert turns("claude", path=claude) == 2
    assert turns("step", path=step) == 2
    assert turns("codex", path=codex) == 2
    assert turns("kimi", dir_=kimi_dir) == 2


def test_a_claude_tool_result_is_not_a_turn(tmp_path):
    """claude stores a tool result as a user record; counting records made a
    session with two questions and a dozen tool calls look like a marathon."""
    path = write_claude(tmp_path, "c1", [claude_user("one"), claude_tool_result(),
                                         claude_tool_result(), claude_user("two"),
                                         claude_tool_result()])
    assert turns("claude", path=path) == 2


def test_a_pasted_transcript_is_one_turn_not_many(tmp_path):
    """Lines are counted, not occurrences: a user message that pastes another
    session's log quotes its role markers verbatim."""
    path = write_codex(tmp_path, "x1", [codex_user('pasted: {"role":"user"} {"role":"user"}'),
                                        codex_user("two")])
    assert turns("codex", path=path) == 2


def test_turns_are_unknown_rather_than_zero_without_markers(tmp_path):
    """A store that writes its JSON with spaces after the colons matches
    nothing; 0 would read as "this session had no turns"."""
    path = tmp_path / "spaced.jsonl"
    path.write_text('{"type": "user", "message": {}}\n', encoding="utf-8")
    assert turns("claude", path=path) is None


@pytest.mark.parametrize("record", [
    {"tool": "claude", "path": "/no/such/file.jsonl"},
    {"tool": "claude"},                       # no path at all
    {"tool": "codex"},                        # codex records once carried none
    {"tool": "kimi"},
    {"tool": "not-a-tool", "path": "/x"},
])
def test_turns_are_unknown_when_they_cannot_be_counted(record):
    assert sessions.session_turns(record) is None


# ---------- the current-directory marker ----------

def _rows():
    return [("step", "id-1", "1h ago", "id-1", "/work/a/really/quite/long/directory/name", "Alpha"),
            ("step", "id-2", "2h ago", "id-2", "/work/another/equally/long/directory", "Beta")]


def test_the_marker_marks_the_rows_that_belong_here(monkeypatch, tmp_path):
    _plain(monkeypatch)
    mine = tmp_path / "mine"
    other = tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    monkeypatch.setattr(os, "getcwd", lambda: str(mine))
    stream = _capture(monkeypatch)
    rows = [("step", "id-1", "1h ago", "id-1", str(mine), "Alpha"),
            ("step", "id-2", "2h ago", "id-2", str(other), "Beta")]
    sessions.render_rows(rows, write_cache=False)

    lines = _printed(stream).splitlines()
    assert lines[1].startswith("▸")
    assert lines[2].startswith("  ")


def test_the_marker_falls_back_when_the_console_cannot_draw_it(monkeypatch, tmp_path):
    """A GBK console has neither ▸ nor »: printing one substitutes "?", which
    reads as a bug. ">" says the same thing in ASCII."""
    _plain(monkeypatch)
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
    stream = _capture(monkeypatch, encoding="gbk")
    sessions.render_rows([("step", "id-1", "1h ago", "id-1", str(tmp_path), "Alpha")],
                         write_cache=False)

    assert _printed(stream).splitlines()[1].startswith(">")


def test_an_ellipsis_the_console_cannot_draw_becomes_dots(monkeypatch):
    """Same rule for the clip glyph: an ASCII console gets ".." and the cell
    is measured to fit it."""
    _plain(monkeypatch)
    monkeypatch.setenv("COLUMNS", "50")
    stream = _capture(monkeypatch, encoding="ascii")
    sessions.render_rows([("step", "id-1", "1h ago", "id-1", "/a/very/long/path/here", "Alpha")],
                         write_cache=False,
                         sources={"id-1": {"tool": "step", "path": "/no/such/file.jsonl"}})

    line = _printed(stream).splitlines()[1]
    assert ".." in line
    assert "…" not in line


# ---------- columns that measure what they print ----------

def test_a_cjk_title_keeps_the_columns_lined_up(monkeypatch):
    """len() counts code points, so a Chinese title measured half of what it
    printed and pushed every field after it out of line."""
    _plain(monkeypatch)
    stream = _capture(monkeypatch)
    sessions.render_rows([("step", "id-1", "1h ago", "id-1", "/work", "修复 GBK 控制台崩溃"),
                          ("codex", "id-2", "2h ago", "id-2", "/work", "plain ascii title")],
                         write_cache=False)

    lines = _printed(stream).splitlines()
    header, first, second = lines[0], lines[1], lines[2]
    # the title starts at the same display column on both rows and the header
    assert (sessions._display_width(header) - sessions._display_width("TITLE")
            == sessions._display_width(first) - sessions._display_width("修复 GBK 控制台崩溃")
            == sessions._display_width(second) - sessions._display_width("plain ascii title"))


def test_the_turns_column_yields_to_a_narrow_terminal(monkeypatch):
    _plain(monkeypatch)
    rows = _rows()
    monkeypatch.setenv("COLUMNS", "120")
    wide = _capture(monkeypatch)
    sessions.render_rows(rows, write_cache=False, sources={"id-1": {"tool": "step"},
                                                           "id-2": {"tool": "step"}})
    assert "TURNS" in _printed(wide).splitlines()[0]

    monkeypatch.setenv("COLUMNS", "50")
    narrow = _capture(monkeypatch)
    sessions.render_rows(rows, write_cache=False, sources={"id-1": {"tool": "step"},
                                                           "id-2": {"tool": "step"}})
    lines = _printed(narrow).splitlines()
    assert "TURNS" not in lines[0]  # the title keeps the room instead
    assert "…" in lines[1]          # and the cwd gave up its head


def test_a_row_whose_file_is_gone_shows_a_question_mark(monkeypatch):
    _plain(monkeypatch)
    stream = _capture(monkeypatch)
    sessions.render_rows(_rows(), write_cache=False,
                         sources={"id-1": {"tool": "step", "path": "/no/such/file.jsonl"},
                                  "id-2": {"tool": "step", "path": "/no/such/file.jsonl"}})

    for line in _printed(stream).splitlines()[1:]:
        assert line.split()[-2] == "?"


# ---------- the listing counts turns for the rows it prints ----------

def test_cmd_list_counts_turns_for_the_rows_it_prints(monkeypatch, tmp_path, capsys):
    """The whole point of the column: `ai` alone, against the real readers."""
    _plain(monkeypatch)
    claude = write_claude(tmp_path, "c1", [claude_user("alpha"), claude_tool_result(),
                                           claude_user("beta")])
    step = write_step(tmp_path, "s1", [{"type": "session", "id": "s1"}, step_user("gamma")])
    codex = write_codex(tmp_path, "x1", [codex_user("one"), codex_user("two"),
                                         codex_user("three")])

    monkeypatch.setattr(sessions, "claude_light_records",
                        lambda: [{"tool": "claude", "id": "c1", "ts": 300,
                                  "path": str(claude), "cwd": "/work"}])
    monkeypatch.setattr(sessions, "codex_light_records",
                        lambda: [{"tool": "codex", "id": "x1", "ts": 200,
                                  "path": str(codex), "title": "Gamma", "cwd": "/work"}])
    monkeypatch.setattr(sessions, "kimi_light_records", lambda show_all: [])
    monkeypatch.setattr(sessions, "step_light_records",
                        lambda show_all=False: [{"tool": "step", "id": "s1", "ts": 100,
                                                 "path": str(step), "cwd": "/work"}])
    monkeypatch.setattr(sessions, "zcode_light_records", lambda show_all=False: [])
    monkeypatch.setattr(sessions, "codebuddy_light_records", lambda show_all=False: [])

    sessions.cmd_list([])

    lines = capsys.readouterr().out.splitlines()
    # header, then one row per session, newest first
    assert lines[0].split()[-2:] == ["TURNS", "TITLE"]
    row_lines = [l for l in lines[1:] if re.match(r"^\s*\d+\s", l)]
    turns_of = {line.split()[-1]: line.split()[-2] for line in row_lines}
    assert turns_of == {"alpha": "2", "gamma": "1", "Gamma": "3"}  # tool result not counted


# ---------- color ----------

class FakeStream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.mark.parametrize("env,isatty,expected", [
    ({}, False, False),                       # a pipe: never
    ({"NO_COLOR": "1"}, True, False),         # the environment said no
    ({"TERM": "dumb"}, True, False),          # ...and so did the terminal
    ({"CROSSWEAVE_COLOR": "never"}, True, False),
    ({"CROSSWEAVE_COLOR": "always"}, False, True),   # asked for, so a pipe gets it too
    ({"CROSSWEAVE_COLOR": "never"}, False, False),
])
def test_color_is_only_emitted_when_the_terminal_asks_for_it(monkeypatch, env, isatty, expected):
    for name in ("NO_COLOR", "TERM", "CROSSWEAVE_COLOR"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert color._decide(FakeStream(isatty)) is expected


def test_a_real_terminal_is_probed_for_vt_support(monkeypatch):
    """A TTY with a clean environment reaches the Windows probe -- whose
    answer is the platform's, so only the call is pinned here."""
    for name in ("NO_COLOR", "TERM", "CROSSWEAVE_COLOR"):
        monkeypatch.delenv(name, raising=False)
    assert isinstance(color._decide(FakeStream(True)), bool)


def test_render_rows_paints_the_tool_column(monkeypatch):
    monkeypatch.setattr(color, "_STATE", True)
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = _capture(monkeypatch)

    sessions.render_rows([("step", "id-1", "1h ago", "id-1", "/work", "Alpha")], write_cache=False)

    out = _printed(stream)
    assert "\x1b[141m" in out      # step's hue
    assert "\x1b[2m" in out        # the plumbing (id, when) is dim
    assert "\x1b[1m" in out        # the header is bold


def test_render_rows_is_plain_when_color_is_off(monkeypatch):
    monkeypatch.setattr(color, "_STATE", False)
    stream = _capture(monkeypatch)

    sessions.render_rows(_rows(), write_cache=False)

    assert "\x1b" not in _printed(stream)


# ---------- where each session left off ----------

def test_the_last_line_shows_where_the_session_ended(monkeypatch, tmp_path):
    """The title says where a session started; this says where it got to."""
    _plain(monkeypatch)
    path = write_step(tmp_path, "s1", [{"type": "session", "id": "s1"},
                                       step_user("fix the nfc lock"),
                                       {"type": "message", "message": {"role": "assistant",
                                                                       "content": "Patched the HAL and rebuilt."}}])
    stream = _capture(monkeypatch)

    sessions.render_rows([("step", "id-1", "1h ago", "id-1", "/work", "fix the nfc lock")],
                         write_cache=False, sources={"id-1": {"tool": "step", "path": str(path)}})

    lines = _printed(stream).splitlines()
    assert len(lines) == 3  # header + row + the last line
    assert lines[2].strip().startswith("└")
    assert "Patched the HAL" in lines[2]


def test_a_one_turn_session_does_not_repeat_its_title(monkeypatch, tmp_path):
    _plain(monkeypatch)
    path = write_step(tmp_path, "s1", [{"type": "session", "id": "s1"},
                                       step_user("just a question")])
    stream = _capture(monkeypatch)

    sessions.render_rows([("step", "id-1", "1h ago", "id-1", "/work", "just a question")],
                         write_cache=False, sources={"id-1": {"tool": "step", "path": str(path)}})

    assert len(_printed(stream).splitlines()) == 2  # header + row, nothing under it


def test_a_trailing_reminder_is_skipped_for_the_real_last_words(monkeypatch, tmp_path):
    """A scheduled task fires its reminder after the work is done; showing it
    as "where the session left off" would name the reminder, not the work."""
    _plain(monkeypatch)
    path = write_step(tmp_path, "s1", [
        {"type": "session", "id": "s1"},
        step_user("do the thing"),
        {"type": "message", "message": {"role": "assistant", "content": "All done."}},
        {"type": "message", "message": {"role": "user",
                                        "content": "<system-reminder>scheduled task fired</system-reminder>"}},
    ])
    stream = _capture(monkeypatch)

    sessions.render_rows([("step", "id-1", "1h ago", "id-1", "/work", "do the thing")],
                         write_cache=False, sources={"id-1": {"tool": "step", "path": str(path)}})

    assert "All done." in _printed(stream).splitlines()[2]


def test_the_last_line_is_absent_without_sources(monkeypatch, tmp_path):
    """Search rows carry the judge's WHY instead -- there is no room for both."""
    _plain(monkeypatch)
    path = write_step(tmp_path, "s1", [{"type": "session", "id": "s1"}, step_user("q"),
                                       {"type": "message", "message": {"role": "assistant",
                                                                       "content": "an answer"}}])
    stream = _capture(monkeypatch)

    sessions.render_rows([("step", "id-1", "1h ago", "id-1", "/work", "q")], write_cache=False)

    assert len(_printed(stream).splitlines()) == 2


def test_the_last_message_comes_from_each_tools_own_shape(monkeypatch, tmp_path):
    claude = write_claude(tmp_path, "c1", [
        claude_user("one"),
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": "claude's last word"}]}},
    ])
    step = write_step(tmp_path, "s1", [
        {"type": "session", "id": "s1"},
        step_user("one"),
        {"type": "message", "message": {"role": "assistant", "content": "step's last word"}},
        {"type": "message", "message": {"role": "toolResult", "content": "ignored"}},
    ])
    codex = write_codex(tmp_path, "x1", [
        codex_user("one"),
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text",
                                                           "text": "codex's last word"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
                                              "arguments": "{}"}},
    ])
    write_kimi(tmp_path, "k1", [
        kimi_prompt("one"),
        kimi_reply("kimi's last word"),
    ])

    assert sessions.session_last_message({"tool": "claude", "path": str(claude)}) == "claude's last word"
    assert sessions.session_last_message({"tool": "step", "path": str(step)}) == "step's last word"
    assert sessions.session_last_message({"tool": "codex", "path": str(codex)}) == "codex's last word"
    assert sessions.session_last_message(
        {"tool": "kimi", "dir": str(tmp_path / ".kimi-code" / "k1")}) == "kimi's last word"


def test_a_codex_last_message_resolves_through_the_path_index(monkeypatch, tmp_path):
    """The listing hands the renderer a light record, and a codex record's
    transcript is found the way the rest of the module finds it."""
    sid = "019ffdbe-1234-7abc-8def-0000000000aa"
    monkeypatch.setattr(common, "CODEX_HOME", str(tmp_path / ".codex"))
    codex._codex_path_index = None
    try:
        path = write_codex(tmp_path, sid, [
            {"type": "session_meta", "payload": {"id": sid, "cwd": "/work"}},
            codex_user("one"),
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text",
                                                               "text": "found through the index"}]}},
        ])
        assert sessions.session_last_message({"tool": "codex", "id": sid}) == "found through the index"
    finally:
        codex._codex_path_index = None


def test_a_missing_transcript_has_no_last_message(tmp_path):
    assert sessions.session_last_message({"tool": "step", "path": str(tmp_path / "gone.jsonl")}) is None
    assert sessions.session_last_message({"tool": "kimi"}) is None
