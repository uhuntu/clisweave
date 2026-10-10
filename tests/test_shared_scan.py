"""One read of a rollout, shared by every question asked of it.

`ai search` asks each codex session for its title, its snippet, its cwd and,
for a handoff or a fork, its parent thread. Each of those used to read and
json-parse the rollout again from scratch: three full passes over a 220MB
store per search, most of what the command cost.

The scan is now shared, which makes the *results* the interesting thing to pin
down -- every codex title heuristic in this repo is the product of a past
wrong answer, so the filter over the shared scan has to keep producing exactly
what the separate readers produced before.
"""

import json

import pytest

from crossweave import codex, common, sessions


@pytest.fixture(autouse=True)
def _reset_codex_caches():
    codex._codex_path_index = None
    codex._codex_scan_cache.clear()
    yield
    codex._codex_path_index = None
    codex._codex_scan_cache.clear()


ROLLOUT = "019ffdbe-1234-7abc-8def-0000000000aa"


def write_rollout(monkeypatch, tmp_path, sid, lines):
    monkeypatch.setattr(common, "CODEX_HOME", str(tmp_path / ".codex"))
    day_dir = tmp_path / ".codex" / "sessions" / "2026" / "08" / "15"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-2026-08-15T10-33-27-{sid}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def meta(sid, cwd="/work/proj", parent=None):
    payload = {"id": sid, "cwd": cwd}
    if parent:
        payload["parent_thread_id"] = parent
    return {"type": "session_meta", "payload": payload}


def message(role, text):
    return {"type": "response_item", "payload": {
        "type": "message", "role": role,
        "content": [{"type": "input_text", "text": text}]}}


def tool_call(command):
    return {"type": "response_item", "payload": {
        "type": "function_call", "name": "shell",
        "arguments": json.dumps({"command": command})}}


def test_the_transcript_is_scanned_once_for_four_readers(monkeypatch, tmp_path):
    """Title, snippet, cwd and parent all come out of one read of the file.

    The file is still opened twice more, for the two session_meta questions
    (cwd, parent) -- that is a two-line read and not worth folding in. What
    the scan stops is the four separate passes over every line of the
    transcript."""
    write_rollout(monkeypatch, tmp_path, ROLLOUT, [
        meta(ROLLOUT), message("user", "fix the nfc lock")])
    scans = []
    real_scan = sessions._codex_scan

    def counting_scan(*args, **kwargs):
        scans.append(args)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(codex, "_codex_scan", counting_scan)
    opens = []
    real_open_text = common.open_text

    def counting_open_text(path):
        opens.append(path)
        return real_open_text(path)

    monkeypatch.setattr(common, "open_text", counting_open_text)

    sessions.codex_rollout_title(ROLLOUT)
    opens_after_title = len(opens)

    sessions.codex_rollout_snippet(ROLLOUT)

    # both transcript readers ask the scan, and the second one reads nothing
    assert len(scans) == 2
    assert opens_after_title == 1
    assert len(opens) == opens_after_title

    # cwd and parent still read the session_meta themselves -- two lines, and
    # not worth folding into the scan
    sessions.codex_cwd(ROLLOUT)
    sessions.codex_parent_thread_id(ROLLOUT)
    assert len(opens) == 3


def test_the_scan_serves_both_the_title_and_the_snippet(monkeypatch, tmp_path):
    """Boilerplate is flagged rather than dropped, because the two readers want
    opposite things from it: the title and the snippet skip it, while the seed
    fallback that names a session a tool started for itself has to see it."""
    write_rollout(monkeypatch, tmp_path, ROLLOUT, [
        meta(ROLLOUT),
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "# AGENTS.md\n\nbe terse"}]}},
        message("user", "the real request"),
        tool_call("aria2c https://example.com/x.tgz"),
        message("assistant", "downloaded it"),
    ])

    assert sessions.codex_rollout_title(ROLLOUT) == "the real request"
    snippet = sessions.codex_rollout_snippet(ROLLOUT)
    assert "the real request" in snippet
    assert "downloaded it" in snippet
    # a tool call is snippet material but never a title
    assert "aria2c" in snippet
    assert "AGENTS.md" not in snippet


def test_the_scan_keeps_a_sessions_own_shape(monkeypatch, tmp_path):
    """Tool calls and boilerplate are recorded in place, in file order, so the
    title (the first genuine message) and the snippet (a stride sample) both
    come out in the order the conversation happened."""
    write_rollout(monkeypatch, tmp_path, ROLLOUT, [
        meta(ROLLOUT),
        message("user", "one"),
        tool_call("echo two"),
        message("assistant", "three"),
    ])
    entries = sessions._codex_scan(sessions.codex_rollout_path(ROLLOUT))
    assert [e[0] for e in entries] == ["message", "tool", "message"]
    assert entries[1] == ("tool", "echo two")
    assert entries[0][1] == "user" and entries[0][3] is False


def test_a_rewritten_rollout_is_reread(monkeypatch, tmp_path):
    """A resumed thread rewrites the store and a test rewrites the same path,
    so a cache keyed by path alone would serve the old transcript forever."""
    path = write_rollout(monkeypatch, tmp_path, ROLLOUT, [
        meta(ROLLOUT), message("user", "first version")])
    assert sessions.codex_rollout_title(ROLLOUT) == "first version"

    # same mtime, same size, different content -- the size check is what
    # catches this one
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(meta(ROLLOUT)) + "\n")
        fh.write(json.dumps(message("user", "second version, longer")) + "\n")

    assert sessions.codex_rollout_title(ROLLOUT) == "second version, longer"


def test_the_cache_holds_the_extracted_texts_not_the_records(monkeypatch, tmp_path):
    """The point of caching extracted texts: a 220MB store costs kilobytes per
    session, rather than the megabytes of parsed rollout it came from."""
    write_rollout(monkeypatch, tmp_path, ROLLOUT, [
        meta(ROLLOUT), message("user", "x" * 5000), tool_call("echo " + "y" * 500)])
    path = sessions.codex_rollout_path(ROLLOUT)
    entries = sessions._codex_scan(path)

    assert [e[2] for e in entries if e[0] == "message"] == ["x" * 5000]
    assert entries[-1] == ("tool", "echo " + "y" * 500)
    # nothing of the surrounding payload structure is retained
    assert len(json.dumps(entries, default=str)) < 7000
    assert entries  # and the same object is handed back next time
    assert sessions._codex_scan(path) is entries


def test_an_unreadable_rollout_scans_as_empty(monkeypatch, tmp_path):
    write_rollout(monkeypatch, tmp_path, ROLLOUT, [meta(ROLLOUT)])
    path = sessions.codex_rollout_path(ROLLOUT)

    def unreadable(p):
        raise PermissionError(p)

    monkeypatch.setattr(common, "open_text", unreadable)

    assert sessions._codex_scan(path) == []
    assert sessions.codex_rollout_snippet(ROLLOUT) == ""
    assert sessions.codex_rollout_title(ROLLOUT) == "(no title)"
