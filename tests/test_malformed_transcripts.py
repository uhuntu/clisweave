"""Transcripts written by *other* tools, some of them still running.

Every file this package reads belongs to claude, codex or kimi, so it can be
mid-write, carry a pasted binary, hold an explicit JSON null where a message
should be, or be truncated in the middle of a multi-byte character. None of
that is an error worth dying over: the tools that write these files are free
to change shape without warning, and a wrapper that crashes on one odd
record takes `ai`, `ai search`, `ai resume` and every handoff down with it
(and then cannot list to tell you which file did it).

These tests hold each reader to that contract.
"""

import json

import pytest

from clisweave import codex, common, sessions


@pytest.fixture(autouse=True)
def _reset_codex_path_cache():
    # codex_rollout_path() caches CODEX_HOME's rollout listing in a
    # module-level global; reset it so one test's tmp_path can't leak.
    codex._codex_path_index = None
    yield
    codex._codex_path_index = None


def claude_session(tmp_path, lines, name="sess.jsonl", raw=None):
    path = tmp_path / name
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return str(path)


def write_codex_rollout(monkeypatch, tmp_path, sid, lines, raw=None):
    """Write a rollout in the shape codex itself does -- session_meta first,
    then response_items -- and point the module at that CODEX_HOME. Pass
    `raw` to write exact bytes instead (for the non-UTF-8 cases)."""
    monkeypatch.setattr(common, "CODEX_HOME", str(tmp_path / ".codex"))
    day_dir = tmp_path / ".codex" / "sessions" / "2026" / "08" / "14"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-2026-08-14T00-00-00-{sid}.jsonl"
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


# build_codex_path_index() finds a rollout by the session id in its filename,
# and that id is a uuid.
ROLLOUT_1 = "019ffdbe-1234-7abc-8def-000000000001"
ROLLOUT_2 = "019ffdbe-1234-7abc-8def-000000000002"


def codex_message(role, text):
    return {"type": "response_item",
            "payload": {"type": "message", "role": role,
                        "content": [{"type": "input_text", "text": text}]}}


# ---------- bytes the files shouldn't hold but do ----------

def test_claude_title_survives_a_non_utf8_byte(tmp_path):
    """One pasted binary blob (or a half-flushed multi-byte character) used
    to raise UnicodeDecodeError out of claude_title_and_cwd, killing every
    command that lists, searches, resumes or hands off."""
    good = json.dumps({"type": "user", "cwd": "/work/proj", "message": {
        "content": [{"type": "text", "text": "fix the nfc lock"}]}}).encode()
    path = claude_session(
        tmp_path, [],
        raw=good + b"\n"
        + b'{"type":"user","message":{"content":[{"type":"text","text":"\xff\xfe\x80"}}]}\n',
    )
    title, cwd = sessions.claude_title_and_cwd(path, cwd_fallback="/guess")
    assert title == "fix the nfc lock"
    assert cwd == "/work/proj"


def test_claude_snippet_and_handoff_survive_a_non_utf8_byte(tmp_path):
    good = json.dumps({"type": "user",
                       "message": {"content": "look at the aria2c mirror"}}).encode()
    path = claude_session(
        tmp_path, [],
        raw=good + b"\n"
        + b'{"type":"user","message":{"content":[{"type":"text","text":"\xff"}}]}\n',
    )
    assert "aria2c mirror" in sessions.claude_snippet(path)
    assert sessions.claude_handoff_messages(path)


def test_codex_readers_survive_a_non_utf8_byte(monkeypatch, tmp_path):
    good = json.dumps(codex_message("user", "tune the engine")).encode()
    write_codex_rollout(monkeypatch, tmp_path, ROLLOUT_1, [], raw=(
        json.dumps({"type": "session_meta",
                    "payload": {"id": ROLLOUT_1, "cwd": "/work"}}).encode()
        + b"\n" + good + b"\n"
        + b'{"type":"response_item","payload":{"type":"message","content":"\xff\xfe"}}\n'))

    assert sessions.codex_rollout_title(ROLLOUT_1) == "tune the engine"
    assert "tune the engine" in sessions.codex_rollout_snippet(ROLLOUT_1)


def test_kimi_readers_survive_a_non_utf8_byte(tmp_path):
    sess = tmp_path / "kimi-sess"
    (sess / "agents" / "main").mkdir(parents=True)
    good = json.dumps({"type": "turn.prompt",
                       "input": [{"type": "text", "text": "ship the kimi patch"}]}).encode()
    (sess / "agents" / "main" / "wire.jsonl").write_bytes(
        good + b"\n" + b'{"type":"turn.prompt","input":[{"type":"text","text":"\xff"}]}\n')
    assert sessions.kimi_title(str(sess)) == "ship the kimi patch"
    assert "kimi patch" in sessions.kimi_snippet(str(sess))
    assert sessions.kimi_handoff_messages(str(sess))


# ---------- explicit nulls and wrong-typed fields ----------

@pytest.mark.parametrize("broken", [
    {"type": "user", "message": None},
    {"type": "user", "message": []},
    {"type": "assistant", "message": None},
    {"type": "user", "cwd": {"path": "/elsewhere"}, "message": "real prompt"},
    {"type": "user", "message": {"content": [{"type": "text", "text": ["not", "a", "string"]}]}},
    {"type": "user", "message": {"content": [{"type": "text", "text": None}]}},
    {"type": "user", "message": {"content": None}},
    {"type": "queue-operation", "content": None},
])
def test_claude_readers_skip_records_with_null_or_wrong_typed_fields(tmp_path, broken):
    path = claude_session(tmp_path, [
        {"type": "user", "cwd": "/work/proj", "message": {"content": "real request"}},
        broken,
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "real answer"}]}},
    ])
    title, cwd = sessions.claude_title_and_cwd(path, cwd_fallback="/guess")
    assert title == "real request"
    assert cwd == "/work/proj"
    assert "real answer" in sessions.claude_snippet(path)
    messages = dict(sessions.claude_handoff_messages(path))
    assert messages["user"] == "real request"
    assert messages["assistant"] == "real answer"


def test_claude_title_ignores_a_stored_title_that_is_not_a_string(tmp_path):
    path = claude_session(tmp_path, [
        {"type": "user", "cwd": "/work", "message": {"content": "the actual question"}},
        {"type": "custom-title", "customTitle": {"nested": "object"}},
    ])
    title, _cwd = sessions.claude_title_and_cwd(path, cwd_fallback="/guess")
    assert title == "the actual question"


def test_claude_title_flattens_a_stored_title_with_newlines(tmp_path):
    """Titles are printed straight into a table column."""
    path = claude_session(tmp_path, [
        {"type": "user", "cwd": "/work", "message": {"content": "hi"}},
        {"type": "custom-title", "customTitle": "NFC HAL\nOpenAfterOpen\tfix"},
    ])
    title, _cwd = sessions.claude_title_and_cwd(path, cwd_fallback="/guess")
    assert title == "NFC HAL OpenAfterOpen fix"


def test_codex_readers_skip_records_with_null_or_wrong_typed_fields(monkeypatch, tmp_path):
    rollout = write_codex_rollout(monkeypatch, tmp_path, ROLLOUT_2, [
        {"type": "session_meta", "payload": {"id": ROLLOUT_2, "cwd": "/work"}},
        {"type": "response_item", "payload": None},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": None}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": ["x"]}]}},
        codex_message("user", "trace the deadlock"),
    ])
    assert sessions.codex_rollout_title(ROLLOUT_2) == "trace the deadlock"
    assert "trace the deadlock" in sessions.codex_rollout_snippet(ROLLOUT_2)
    assert sessions.codex_handoff_messages(rollout)


def test_kimi_readers_skip_records_with_null_or_wrong_typed_fields(tmp_path):
    sess = tmp_path / "sess"
    (sess / "agents" / "main").mkdir(parents=True)
    (sess / "agents" / "main" / "wire.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in [
            {"type": "turn.prompt", "input": None},
            {"type": "turn.prompt", "input": [{"type": "text", "text": ["also", "wrong"]}]},
            {"type": "context.append_loop_event", "event": None},
            {"type": "context.append_loop_event",
             "event": {"type": "content.part", "part": None}},
            {"type": "context.append_loop_event",
             "event": {"type": "content.part", "part": {"type": "text", "text": None}}},
            {"type": "context.append_loop_event",
             "event": {"type": "tool.result", "result": None}},
            {"type": "context.append_loop_event",
             "event": {"type": "tool.result", "result": {"output": None}}},
            {"type": "turn.prompt",
             "input": [{"type": "text", "text": "the genuine question"}]},
            {"type": "context.append_loop_event",
             "event": {"type": "content.part",
                       "part": {"type": "text", "text": "the genuine answer"}}},
        ]))
    assert sessions.kimi_title(str(sess)) == "the genuine question"
    assert "genuine answer" in sessions.kimi_snippet(str(sess))
    messages = dict(sessions.kimi_handoff_messages(str(sess)))
    assert messages["user"] == "the genuine question"
    assert messages["assistant"] == "the genuine answer"


# ---------- the listing must survive to the formatter ----------

def test_resolve_row_survives_a_cwd_that_is_not_a_string():
    """A cwd of {"path": "/x"} reaches render_rows' width formatting, which
    raises on a dict -- one odd record took the whole listing down."""
    row = {"tool": "claude", "id": "abc123", "ts": 1700000000,
           "path": "/x.jsonl", "cwd": {"path": "/x"}}
    resolved = sessions.resolve_row(row)
    assert resolved[0] == "claude"
    assert isinstance(resolved[4], str)


def test_render_rows_prints_a_row_whose_cwd_is_not_a_string(capsys):
    sessions.render_rows([("claude", "abc", "1d ago", "abc", {"path": "/x"}, "a title")],
                         write_cache=False)
    assert "abc" in capsys.readouterr().out


def test_listing_of_an_absent_claude_store_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "CLAUDE_PROJECTS", str(tmp_path / "no-claude"))
    assert sessions.claude_light_records() == []


# ---------- literal search ----------

def test_literal_texts_ignores_entries_that_are_not_objects():
    """A line that parses to a bare scalar is ordinary junk; entry.get() on it
    used to raise, and the exception was swallowed so the session vanished
    from search results without a word."""
    assert sessions._literal_texts("claude", 17) == []
    assert sessions._literal_texts("codex", None) == []
    assert sessions._literal_texts("kimi", "a bare string") == []


def test_literal_texts_survives_null_fields():
    assert sessions._literal_texts("claude", {"type": "user", "message": None}) == []
    assert sessions._literal_texts("codex", {"type": "response_item", "payload": None}) == []
    assert sessions._literal_texts("kimi", {"type": "turn.prompt", "input": None}) == []
    assert sessions._literal_texts("kimi", {"type": "context.append_loop_event", "event": None}) == []


def test_file_contains_finds_a_match_after_a_corrupt_line(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        json.dumps(codex_message("user", "nope")) + "\n"
        '{"type":"response_item","payload":{"type":"message","role":"user",\n'  # cut mid-write
        + json.dumps(codex_message("user", "aria2c mirror")) + "\n"
    )
    assert sessions._file_contains(str(path), sessions._literal_pattern("aria2c"), "codex")
    assert not sessions._file_contains(str(path), sessions._literal_pattern("katago"), "codex")


def test_file_contains_survives_a_line_too_deep_to_parse(tmp_path):
    """400 levels of nesting: json.loads raises RecursionError on it, which is
    not a ValueError, so the reader has to survive a line being unparseable in
    a second way as well."""
    deep = ('{"type":"turn.prompt","input":' + "[" * 400
            + '{"type":"text","text":"buried"}' + "]" * 400 + "}")
    path = tmp_path / "wire.jsonl"
    path.write_text(deep + "\n" + json.dumps(
        {"type": "turn.prompt",
         "input": [{"type": "text", "text": "shallow hit"}]}) + "\n")
    assert sessions._file_contains(str(path), sessions._literal_pattern("shallow"), "kimi")
