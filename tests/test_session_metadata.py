"""Where a session ran, and which copy of it is the current one.

Two failure modes, both of which produced a confidently wrong answer rather
than an error:

* a session's cwd was missing, wrong, or compared with raw string equality,
  so `ai sessions --cwd` (documented as "only sessions started in the current
  directory") filtered out sessions that were, and `ai stats` bucketed them
  under `?`
* a codex thread's several rollout files were ranked by mtime alone, so on a
  tie (1-second filesystem granularity, `cp -p`, rsync, cloud sync) the stale
  one was read: every field came from the thread as it stood before its last
  resume
"""

import json
import os

import pytest

from clisweave import codex, common, sessions


@pytest.fixture(autouse=True)
def _reset_codex_path_cache():
    codex._codex_path_index = None
    yield
    codex._codex_path_index = None


ROLLOUT = "019ffdbe-1234-7abc-8def-0000000000aa"


def write_rollout(monkeypatch, tmp_path, sid, cwd, text, stamp, mtime=None, extra_lines=()):
    monkeypatch.setattr(common, "CODEX_HOME", str(tmp_path / ".codex"))
    year, month, day = stamp[:10].split("-")
    day_dir = tmp_path / ".codex" / "sessions" / year / month / day
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-{stamp}-{sid}.jsonl"
    lines = [json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd}})]
    if text is not None:
        lines.append(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}]}}))
    lines.extend(json.dumps(line) for line in extra_lines)
    path.write_text("\n".join(lines) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---------- a session's cwd ----------

# ---------- a session's cwd ----------

def test_codex_rollout_files_do_not_follow_a_symlinked_directory(monkeypatch, tmp_path):
    """A `sessions/current -> sessions` symlink is a thing people add, and a
    recursive `**` glob follows it: every rollout came back once per nesting
    level, and each of those copies was then stat'ed and opened."""
    write_rollout(monkeypatch, tmp_path, ROLLOUT, "/home/hunt/work", "start the work",
                  "2026-08-14T00-00-00")
    (tmp_path / ".codex" / "sessions" / "current").symlink_to(
        tmp_path / ".codex" / "sessions", target_is_directory=True)

    files = sessions.codex_rollout_files()
    assert len(files) == 1
    assert sessions.codex_light_records()[0]["id"] == ROLLOUT


def test_codex_light_records_carry_the_cwd(monkeypatch, tmp_path):
    """Without one, `ai sessions --cwd` never matched a single codex session
    and `ai stats` reported every one of them under `?`."""
    write_rollout(monkeypatch, tmp_path, ROLLOUT, "/home/hunt/work/cw", "start the work",
                  "2026-08-14T00-00-00")
    records = sessions.codex_light_records()
    assert [r["cwd"] for r in records] == ["/home/hunt/work/cw"]


def test_cwd_filter_matches_codex_sessions(monkeypatch, tmp_path, capsys):
    write_rollout(monkeypatch, tmp_path, ROLLOUT, os.getcwd(), "start the work",
                  "2026-08-14T00-00-00")
    monkeypatch.setattr(sessions, "claude_light_records", lambda: [])
    monkeypatch.setattr(sessions, "kimi_light_records", lambda show_all: [])

    sessions.cmd_list(["--cwd"])
    out = capsys.readouterr().out
    assert "start the work" in out


def test_cwd_filter_compares_paths_not_strings(monkeypatch, tmp_path):
    """/var vs /private/var on macOS, a different case on Windows, a trailing
    separator: all the same directory, and raw equality dropped them all."""
    # a trailing separator, and the same directory reached through a symlink
    assert sessions._same_path("/a/b", "/a/b/")
    # Windows-written cwds compare as Windows would, on any host: case,
    # separators and a trailing one all ignored, `.` collapsed
    assert sessions._same_path("C:\\Work\\proj", "c:/work/proj")
    assert sessions._same_path("C:\\Work\\proj\\", "c:/work/./proj")
    assert sessions._same_path("\\\\srv\\share\\proj", "\\\\SRV\\share\\proj")
    assert not sessions._same_path("C:\\Work\\proj", "c:/work/other")
    assert not sessions._same_path("/a/b", "/a/c")
    assert not sessions._same_path(None, "/a/b")
    assert not sessions._same_path({"path": "/a"}, "/a")


def test_claude_project_dir_decode_handles_a_windows_drive(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    proj_dir = projects / "C--Users-hunt-work-proj"
    proj_dir.mkdir(parents=True)
    (proj_dir / "abcd1234.jsonl").write_text(json.dumps(
        {"type": "user", "message": {"content": "fix the nfc lock"}}) + "\n")
    monkeypatch.setattr(common, "CLAUDE_PROJECTS", str(projects))

    records = sessions.claude_light_records()
    assert [r["cwd"] for r in records] == ["C:/Users/hunt/work/proj"]


def test_claude_project_dir_decode_still_handles_posix():
    assert sessions.decode_project_dir_name("-home-hunt--openclaw-workspace") == "/home/hunt/openclaw/workspace"


# ---------- which rollout is the current one ----------
def test_newest_rollout_wins_an_mtime_tie(monkeypatch, tmp_path):
    """A resumed thread owns a rollout per resume; equal mtimes (cp -p,
    rsync, cloud sync) used to leave the older one selected, so the row's
    title, cwd and handoff transcript all came from before the resume."""
    mtime = 1_700_000_000
    write_rollout(monkeypatch, tmp_path, ROLLOUT, "/old", "start the work",
                  "2026-08-14T00-00-00", mtime=mtime)
    write_rollout(monkeypatch, tmp_path, ROLLOUT, "/new", "resume the work",
                  "2026-08-15T10-33-27", mtime=mtime)

    assert sessions.codex_rollout_path(ROLLOUT).endswith("rollout-2026-08-15T10-33-27-" + ROLLOUT + ".jsonl")
    assert sessions.codex_rollout_title(ROLLOUT) == "resume the work"
    assert sessions.codex_cwd(ROLLOUT) == "/new"


def test_codex_cwd_looks_past_a_corrupt_first_line(monkeypatch, tmp_path):
    """A file read while codex is still writing it opens with a truncated
    record; stopping at the first *parseable* line cost the session its cwd
    (and `ai resume` its chdir)."""
    write_rollout(monkeypatch, tmp_path, ROLLOUT, "/home/hunt/work", "tune the engine",
                  "2026-08-14T00-00-00")
    path = sessions.codex_rollout_path(ROLLOUT)
    with open(path, encoding="utf-8") as fh:
        original = fh.read()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"type":"session_meta","payload":{"id":"trunc\n')  # cut mid-write
        fh.write(original)

    assert sessions.codex_cwd(ROLLOUT) == "/home/hunt/work"


def test_codex_parent_thread_id_looks_past_a_corrupt_first_line(monkeypatch, tmp_path):
    parent = "019ffdbe-1234-7abc-8def-0000000000bb"
    write_rollout(monkeypatch, tmp_path, ROLLOUT, "/home/hunt/work", "forked work",
                  "2026-08-14T00-00-00")
    path = sessions.codex_rollout_path(ROLLOUT)
    with open(path, encoding="utf-8") as fh:
        original = fh.read()
    # a fork's own session_meta carries the parent, behind a truncated record
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"type":"session_meta","payload":{"id":"trunc\n')
        fh.write(json.dumps({"type": "session_meta", "payload": {
            "id": ROLLOUT, "cwd": "/home/hunt/work",
            "parent_thread_id": parent}}) + "\n")
        fh.write(original)

    assert sessions.codex_parent_thread_id(ROLLOUT) == parent


def test_codex_thread_names_keeps_the_latest_name_when_updated_at_is_unparseable(monkeypatch, tmp_path):
    """The index is append-only, so the last line for an id is its newest
    name -- but an unparseable updated_at made every entry tie at 0, and the
    *first* line won."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "session_index.jsonl").write_text("\n".join([
        json.dumps({"id": ROLLOUT, "thread_name": "Old name", "updated_at": None}),
        json.dumps({"id": ROLLOUT, "thread_name": "Renamed to something real",
                    "updated_at": "not a timestamp"}),
    ]) + "\n")
    monkeypatch.setattr(common, "CODEX_HOME", str(codex_home))

    assert sessions.codex_thread_names()[ROLLOUT] == "Renamed to something real"


def test_codex_thread_names_still_prefers_the_newest_timestamp(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "session_index.jsonl").write_text("\n".join([
        json.dumps({"id": ROLLOUT, "thread_name": "the real name",
                    "updated_at": "2026-08-15T10:33:27.000Z"}),
        json.dumps({"id": ROLLOUT, "thread_name": "a stale earlier line",
                    "updated_at": "2026-08-14T00:00:00.000Z"}),
    ]) + "\n")
    monkeypatch.setattr(common, "CODEX_HOME", str(codex_home))

    assert sessions.codex_thread_names()[ROLLOUT] == "the real name"


# ---------- kimi's several timestamp schemas ----------

@pytest.mark.parametrize("value,expected", [
    (1755161559000, 1755161559),          # epoch milliseconds (current)
    (1755161559, 1755161559),             # epoch *seconds*, read as ms before
    ("1755161559000", 1755161559),
    ("2026-08-14T00:00:00.177Z", 1786665600),
    (None, 0),
    ("", 0),
    (True, 0),
    ("not a timestamp", 0),
])
def test_parse_kimi_timestamp(value, expected):
    assert sessions.parse_kimi_timestamp(value) == expected


def test_kimi_light_records_sort_epoch_second_sessions_correctly(monkeypatch, tmp_path):
    """An epoch-seconds updatedAt divided by 1000 landed in 1970, so the
    session sorted to the bottom of every listing as "20833d ago"."""
    kimi_home = tmp_path / ".kimi-code"
    kimi_home.mkdir()
    sess_dir = tmp_path / "sessdir"
    sess_dir.mkdir()
    (sess_dir / "state.json").write_text(json.dumps({"updatedAt": 1755161559}))
    (kimi_home / "session_index.jsonl").write_text(json.dumps(
        {"sessionId": "session_x", "sessionDir": str(sess_dir)}) + "\n")
    monkeypatch.setattr(common, "KIMI_HOME", str(kimi_home))

    records = sessions.kimi_light_records(show_all=False)
    assert records[0]["ts"] == 1755161559
    assert sessions.relative_time(records[0]["ts"]) != "?"


# ---------- the resume cache ----------

def test_read_list_cache_drops_malformed_entries(monkeypatch, tmp_path):
    """A truncated cache surfaced as a KeyError traceback instead of the
    friendly "run `ai sessions` first" the empty case already gives."""
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    (tmp_path / "last_list.json").write_text(json.dumps([
        {"tool": "claude", "id": "abc"},
        {"tool": "claude"},
        ["claude"],
        "claude",
        {"tool": "codex", "id": "def"},
    ]))
    assert sessions.read_list_cache() == [
        {"tool": "claude", "id": "abc"},
        {"tool": "codex", "id": "def"},
    ]
