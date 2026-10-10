"""zcode, the sixth store: the ZCode desktop app's SQLite database.

ZCode is the odd one out -- there is no per-session transcript file and no
CLI that reopens a session. The conversation lives in `~/.zcode/cli/db/
db.sqlite` as `session` / `message` / `part` rows, the cwd is
`session.directory`, and the title is one ZCode's own auto-titler keeps
current. These tests build a synthetic store of exactly the shapes the
reader queries and hold it to the same contract the file-backed tools meet.
"""

import json
import os
import sqlite3

import pytest

from clisweave import cli, codebuddy, common, search, sessions, step, zcode

SID = "sess_90bdabce-9550-4a6f-b46f-440ec7627594"
SID2 = "sess_49792350-31fc-4299-909f-f760ca2bcfe9"
SUB = "sess_subagent_agent_2705b396-d823-44b7-b60f-a75aa888f878"
CWD = "/home/hunt/work/JSearch"

SCHEMA = """
CREATE TABLE session (id TEXT, directory TEXT, title TEXT,
                      time_created INTEGER, time_updated INTEGER, parent_id TEXT);
CREATE TABLE message (id TEXT, session_id TEXT, sequence INTEGER, data TEXT);
CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, sequence INTEGER, data TEXT);
"""

MS = 1_791_445_523_543  # epoch ms, the store's unit


@pytest.fixture(autouse=True)
def _isolate_zcode_store(monkeypatch, tmp_path):
    monkeypatch.setattr(zcode, "ZCODE_DB", str(tmp_path / "no-zcode.sqlite"))
    monkeypatch.setattr(zcode, "_json1", None)  # the probe caches per process
    # the machine's own codebuddy store would leak real sessions into the
    # listing/search tests here that gather all the tools
    monkeypatch.setattr(codebuddy, "CODEBUDDY_PROJECTS",
                        str(tmp_path / "no-codebuddy"))
    yield


class Store:
    """A synthetic db.sqlite built row by row the way the reader queries it."""

    def __init__(self, path):
        self.con = sqlite3.connect(path)
        self.con.executescript(SCHEMA)
        self.n = 0

    def session(self, sid, cwd=CWD, title=None, created=MS, updated=MS + 5_000, parent=None):
        self.con.execute("INSERT INTO session VALUES (?,?,?,?,?,?)",
                         (sid, cwd, title, created, updated, parent))
        return self

    def message(self, mid, sid, role, seq):
        self.con.execute("INSERT INTO message VALUES (?,?,?,?)",
                         (mid, sid, seq, json.dumps({"role": role})))
        return self

    def part(self, sid, mid, data, seq=None):
        self.n += 1
        self.con.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                         ("p%d" % self.n, mid, sid, seq if seq is not None else self.n,
                          json.dumps(data)))
        return self

    def text(self, sid, mid, text, seq=None):
        return self.part(sid, mid, {"type": "text", "text": text}, seq)

    def done(self):
        self.con.commit()
        self.con.close()
        return self


def build_db(tmp_path, name="db.sqlite"):
    """A store at `<tmp>/<name>`, wired in directly (the step pattern: a
    direct assignment, not a monkeypatch -- the autouse fixture already
    reset ZCODE_DB for this test)."""
    path = tmp_path / name
    store = Store(path)
    zcode.ZCODE_DB = str(path)
    return store


def one_session(tmp_path, sid=SID, cwd=CWD, title="Project Visibility Check",
                lines=()):
    """A one-session store whose first message is user text, then whatever
    `lines` holds (each a (role, [part, ...]) pair)."""
    store = build_db(tmp_path)
    store.session(sid, cwd=cwd, title=title)
    store.message("m1", sid, "user", 1)
    store.text(sid, "m1", "fix the nfc lock", seq=1)
    for i, (role, parts) in enumerate(lines, start=2):
        mid = "m%d" % i
        store.message(mid, sid, role, i)
        for part in parts:
            store.part(sid, mid, part)
    return store.done()


def zcode_record(sid=SID):
    return next(r for r in zcode.zcode_light_records(show_all=True) if r["id"] == sid)


# ---------- discovery ----------

def test_light_records_read_id_cwd_title_and_times(tmp_path):
    build_db(tmp_path).session(SID, cwd=CWD, title="Project Visibility Check").done()
    records = zcode.zcode_light_records()
    assert len(records) == 1
    r = records[0]
    assert r["tool"] == "zcode"
    assert r["id"] == SID
    assert r["cwd"] == CWD
    assert r["title"] == "Project Visibility Check"
    assert r["ts"] == (MS + 5_000) // 1000
    assert r["started"] == MS // 1000
    assert r["path"] is None  # the transcript is in the store, not a file


def test_a_missing_store_is_an_empty_listing_not_a_crash(tmp_path):
    # ZCODE_DB points at a nonexistent path via the autouse fixture
    assert zcode.zcode_light_records() == []
    assert zcode.zcode_resolve(SID[:8]) == []
    assert zcode.zcode_session_cwd(SID) is None
    assert zcode.zcode_session_turns(SID) is None
    assert zcode.zcode_handoff_messages(SID) == []


def test_a_store_with_the_wrong_schema_reads_as_empty(tmp_path):
    """A future ZCode that reshapes its tables is a store this reader can't
    see -- the treatment an absent file gets, not a traceback."""
    path = tmp_path / "db.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE session (something_else TEXT)")
    con.commit()
    con.close()
    zcode.ZCODE_DB = str(path)
    assert zcode.zcode_light_records() == []


def test_subagent_sessions_stay_hidden_until_all_asks(tmp_path):
    """Sessions with a parent_id are subagent runs ZCode started for itself
    -- step's `subagent-` category -- and hold no conversation of anyone's."""
    build_db(tmp_path).session(SID).session(SUB, parent=SID).done()
    assert [r["id"] for r in zcode.zcode_light_records()] == [SID]
    assert sorted(r["id"] for r in zcode.zcode_light_records(show_all=True)) == sorted([SID, SUB])


def test_resolve_matches_by_prefix(tmp_path):
    build_db(tmp_path).session(SID).session(SID2).done()
    assert zcode.zcode_resolve("sess_90bd") == [SID]
    assert zcode.zcode_resolve(SID) == [SID]
    assert sorted(zcode.zcode_resolve("sess_")) == sorted([SID, SID2])
    assert zcode.zcode_resolve("zzz") == []


def test_ambiguous_prefixes_list_their_matches(tmp_path):
    build_db(tmp_path).session(SID).session(SID2).done()
    assert len(zcode.zcode_resolve("sess_4")) == 1  # only SID2 starts sess_4


# ---------- titles ----------

def test_the_stored_title_wins_without_a_scan(tmp_path):
    one_session(tmp_path)
    assert zcode.zcode_light_records()[0]["title"] == "Project Visibility Check"


def test_an_untitled_session_falls_back_to_its_first_genuine_prompt(tmp_path):
    store = build_db(tmp_path)
    store.session(SID, title=None)
    store.message("m1", SID, "user", 1)
    store.text(SID, "m1", "fix the nfc lock", seq=1)
    store.done()
    assert zcode.zcode_light_records()[0]["title"] == "fix the nfc lock"


def test_injected_and_trivial_openers_do_not_become_the_title(tmp_path):
    """ZCode relays system reminders as user text and records bare
    acknowledgements like any tool; neither names the work."""
    store = build_db(tmp_path)
    store.session(SID, title=None)
    store.message("m1", SID, "user", 1)
    store.text(SID, "m1", "<system-reminder>the todo list reminder</system-reminder>", seq=1)
    store.message("m2", SID, "user", 2)
    store.text(SID, "m2", "ok", seq=2)
    store.message("m3", SID, "user", 3)
    store.text(SID, "m3", "wire up the SearXNG bridge", seq=3)
    store.done()
    assert zcode.zcode_light_records()[0]["title"] == "wire up the SearXNG bridge"


def test_a_session_that_is_all_noise_is_still_listed(tmp_path):
    store = build_db(tmp_path)
    store.session(SID, title=None)
    store.message("m1", SID, "user", 1)
    store.text(SID, "m1", "<system-reminder>nothing but reminders</system-reminder>", seq=1)
    store.done()
    assert zcode.zcode_light_records()[0]["title"] == "(no title)"


def test_title_and_cwd_come_back_together(tmp_path):
    one_session(tmp_path)
    title, cwd = zcode.zcode_title_and_cwd(SID)
    assert title == "Project Visibility Check"
    assert cwd == CWD


# ---------- turns, last message, snippet ----------

def test_turns_count_user_messages(tmp_path):
    store = build_db(tmp_path)
    store.session(SID)
    store.message("m1", SID, "user", 1)
    store.text(SID, "m1", "one")
    store.message("m2", SID, "assistant", 2)
    store.text(SID, "m2", "reply")
    store.message("m3", SID, "user", 3)
    store.text(SID, "m3", "two")
    store.done()
    assert zcode.zcode_session_turns(SID) == 2
    assert sessions.session_turns(zcode_record()) == 2


def test_turns_fall_back_to_parsing_when_this_sqlite_lacks_json1(monkeypatch, tmp_path):
    """Python 3.9/3.10 can bundle a SQLite built without JSON1 -- the role
    test then happens here rather than in the query, with the same answer."""
    one_session(tmp_path, lines=[("assistant", [{"type": "text", "text": "hi"}]),
                                 ("user", [{"type": "text", "text": "two"}])])
    monkeypatch.setattr(zcode, "_json1", False)
    assert zcode.zcode_session_turns(SID) == 2


def test_zero_turns_read_as_none(tmp_path):
    store = build_db(tmp_path)
    store.session(SID)
    store.message("m1", SID, "assistant", 1)
    store.text(SID, "m1", "only the model spoke")
    store.done()
    assert zcode.zcode_session_turns(SID) is None


def test_the_last_message_is_the_last_thing_anyone_said(tmp_path):
    """Tool calls and injected reminders sit between the row and the answer;
    walking backwards past them is the normal case."""
    one_session(tmp_path, lines=[
        ("assistant", [{"type": "tool", "tool": "Bash",
                        "state": {"input": {"command": "grep -v x"}, "output": "done"}}]),
        ("user", [{"type": "text", "text": "<system-reminder>a trailing reminder</system-reminder>"}]),
        ("assistant", [{"type": "reasoning", "thinking": "silent"}]),
        ("assistant", [{"type": "text", "text": "patched the HAL"}]),
    ])
    assert sessions.session_last_message(zcode_record()) == "patched the HAL"


def test_last_message_none_when_only_the_model_ran_tools(tmp_path):
    """No leading user message at all -- the helper's one always speaks, and
    a session that opened with a tool call is exactly the case the row must
    not answer from."""
    store = build_db(tmp_path)
    store.session(SID)
    store.message("m1", SID, "assistant", 1)
    store.part(SID, "m1", {"type": "tool", "tool": "Bash",
                           "state": {"input": {"command": "ls"}, "output": "files"}})
    store.done()
    assert sessions.session_last_message(zcode_record()) is None


def test_the_snippet_spans_speech_and_calls_with_a_budget(tmp_path):
    one_session(tmp_path, lines=[
        ("assistant", [{"type": "text", "text": "patched the HAL"}]),
        ("assistant", [{"type": "tool", "tool": "Bash",
                        "state": {"input": {"command": "git commit -m fix"}, "output": "ok"}}]),
    ])
    snippet = search.snippet_for(zcode_record())
    assert "fix the nfc lock" in snippet
    assert "patched the HAL" in snippet
    assert "git commit -m fix" in snippet
    assert len(snippet) <= 800


# ---------- the literal scan ----------

def test_the_literal_scan_finds_said_and_returned_text(tmp_path):
    one_session(tmp_path, lines=[
        ("assistant", [{"type": "tool", "tool": "Bash",
                        "state": {"input": {"command": "cat JSENGINES.TXT"},
                                  "output": "4 of 4 records"}}]),
    ])
    rec = zcode_record()
    assert [r["id"] for r in sessions.literal_matches([rec], "JSENGINES")] == [SID]
    assert [r["id"] for r in sessions.literal_matches([rec], "4 of 4")] == [SID]
    assert sessions.literal_matches([rec], "nowhere to be found") == []


# ---------- search candidates ----------

def test_zcode_joins_the_search_candidates(monkeypatch, tmp_path):
    for attr in ("CLAUDE_PROJECTS", "CODEX_HOME", "KIMI_HOME"):
        monkeypatch.setattr(common, attr, str(tmp_path / ("no-" + attr.lower())))
    monkeypatch.setattr(step, "STEP_SESSIONS", str(tmp_path / "no-step"))
    store = build_db(tmp_path)
    store.session(SID).session(SUB, parent=SID).done()
    assert [r["id"] for r in search.gather_candidates("zcode")] == [SID]
    assert SID in [r["id"] for r in search.gather_candidates(None)]
    # a subagent row is not a candidate, like `cw sessions` hides it
    assert SUB not in [r["id"] for r in search.gather_candidates(None)]


# ---------- integration with the listing ----------

def test_zcode_rows_appear_in_the_listing(monkeypatch, tmp_path, capsys):
    """The listing is the integration test: a zcode session shows up with the
    other tools' sessions, turns counted, and its own --tool filter works."""
    for attr in ("CLAUDE_PROJECTS", "CODEX_HOME", "KIMI_HOME"):
        monkeypatch.setattr(common, attr, str(tmp_path / ("no-" + attr.lower())))
    monkeypatch.setattr(step, "STEP_SESSIONS", str(tmp_path / "no-step"))
    one_session(tmp_path, lines=[("assistant", [{"type": "text", "text": "patched the HAL"}])])

    sessions.cmd_list(["--tool", "zcode"])
    out = capsys.readouterr().out
    assert "zcode" in out and "Project Visibility Check" in out
    assert "patched the HAL" in out  # the "where it left off" line

    sessions.cmd_list([])
    out = capsys.readouterr().out
    assert "Project Visibility Check" in out
    assert "step" not in out  # the other stores were empty, not merely quiet


def test_stats_counts_zcode_rows(monkeypatch, tmp_path, capsys):
    for attr in ("CLAUDE_PROJECTS", "CODEX_HOME", "KIMI_HOME"):
        monkeypatch.setattr(common, attr, str(tmp_path / ("no-" + attr.lower())))
    monkeypatch.setattr(step, "STEP_SESSIONS", str(tmp_path / "no-step"))
    build_db(tmp_path).session(SID).session(SID2).session(SUB, parent=SID).done()

    sessions.cmd_stats([])
    out = capsys.readouterr().out
    assert "zcode" in out
    zline = next(line for line in out.splitlines() if line.strip().startswith("zcode"))
    assert "3" in zline  # stats is the --all view: subagents count there


# ---------- handoff ----------

def test_handoff_exports_the_conversation_from_zcode(monkeypatch, tmp_path):
    """`cw <N> claude` on a zcode row exports what was said -- tool calls,
    outputs and thinking stay behind -- and seeds claude with it."""
    one_session(tmp_path, lines=[
        ("assistant", [{"type": "text", "text": "patched the HAL"},
                       {"type": "reasoning", "thinking": "the lock was stale"}]),
        ("assistant", [{"type": "tool", "tool": "Bash",
                        "state": {"input": {"command": "git commit"}, "output": "1 file changed"}}]),
    ])
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(tmp_path / "handoffs"))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    sessions.write_list_cache([{"tool": "zcode", "id": SID}])
    calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: calls.append(argv))

    sessions.handoff_by_number(1, "claude", [])

    assert calls and calls[-1][0] == "claude"
    assert calls[-1][1].startswith("Continue the work from this zcode session")
    export = calls[-1][1].split("export at ")[1].split(". First")[0]
    body = open(export, encoding="utf-8").read()
    assert "fix the nfc lock" in body and "patched the HAL" in body
    # the export is what was said, not the tool's output or the model's notes
    assert "1 file changed" not in body
    assert "the lock was stale" not in body


def test_a_handoff_into_zcode_is_refused(monkeypatch, tmp_path, capsys):
    """zcode is a desktop app with no CLI that starts a session -- there is
    nothing to seed, so the command refuses instead of exporting into a void."""
    with pytest.raises(SystemExit):
        sessions.perform_handoff("claude", SID, "zcode", [])
    assert "no CLI that starts a session" in capsys.readouterr().err


def test_the_handoff_details_reach_the_store_directly(tmp_path):
    one_session(tmp_path, lines=[("assistant", [{"type": "text", "text": "patched the HAL"}])])
    details = sessions.session_handoff_details("zcode", SID)
    assert details is not None
    cwd, markdown = details
    assert cwd == CWD
    assert "fix the nfc lock" in markdown and "patched the HAL" in markdown


# ---------- resume ----------

def test_resume_opens_the_workspace_and_says_what_it_cannot_do(monkeypatch, tmp_path, capsys):
    """No CLI and no deep link reopens one session -- the workspace link is
    the closest route, and the notice says so rather than pretending."""
    workdir = tmp_path / "JSearch"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    one_session(tmp_path, cwd=str(workdir))
    launched = []
    monkeypatch.setattr(sessions.subprocess, "Popen", lambda argv: launched.append(argv))

    sessions.cmd_resume(["zcode", SID])

    assert launched and launched[0][0] == "zcode"
    link = launched[0][1]
    assert link.startswith("zcode://workspace/open?path=%2F")
    assert link == sessions.zcode_workspace_link(str(workdir))
    err = capsys.readouterr().err
    assert "pick the session in the app's task list" in err


def test_resume_without_an_id_points_at_the_app(tmp_path, capsys):
    """The app is its own session picker; nothing is launched."""
    one_session(tmp_path)
    sessions.cmd_resume(["zcode"])
    assert "pick the session in its task list" in capsys.readouterr().err


def test_resume_cannot_relocate_a_zcode_session(tmp_path, capsys):
    """Every tool ties a session to its directory, and the other tools'
    answer -- a fresh seeded session in the new one -- needs a CLI zcode
    does not have. Refused, with the directory named."""
    one_session(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(SystemExit):
        sessions.cmd_resume(["zcode", SID, "--cwd", str(other)])
    assert "can't be relocated in place" in capsys.readouterr().err


def test_resume_by_prefix_resolves_through_the_store(monkeypatch, tmp_path):
    workdir = tmp_path / "JSearch"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    one_session(tmp_path, cwd=str(workdir))
    launched = []
    monkeypatch.setattr(sessions.subprocess, "Popen", lambda argv: launched.append(argv))

    sessions.cmd_resume(["zcode", "sess_90bd"])

    assert launched and launched[0][1] == sessions.zcode_workspace_link(str(workdir))


# ---------- the wrapper side ----------

def test_bare_cw_zcode_opens_the_app_here():
    cmd = cli.build_command("zcode", [])
    assert cmd == ["zcode", sessions.zcode_workspace_link(os.getcwd())]


def test_zcode_takes_no_wrapper_flags():
    """No terminal session to attach to and no flags to translate -- the
    error says what to do instead."""
    with pytest.raises(cli.UsageError):
        cli.build_command("zcode", ["-p", "summarize"])
    with pytest.raises(cli.UsageError):
        cli.build_command("zcode", ["-c"])
    with pytest.raises(cli.UsageError):
        cli.build_command("zcode", ["--", "whatever"])
    with pytest.raises(cli.UsageError):
        cli.build_command("zcode", ["/some/dir"])


def test_the_deep_link_encodes_the_whole_path():
    link = zcode.zcode_workspace_link("/home/hunt/work/JSearch")
    assert link == "zcode://workspace/open?path=%2Fhome%2Fhunt%2Fwork%2FJSearch"
