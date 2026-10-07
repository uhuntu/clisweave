"""cb: every CodeBuddy session, across the CLI, the VS Code extension and the
desktop app.

The three clients keep unrelated stores (a jsonl per session, a tree of
conversation directories plus snapshot files, a sqlite key/value table), so
these tests build a synthetic copy of each at the shape a real install has and
hold the readers, the cross-client view and `cb resume` to it.
"""

import base64
import json
import sqlite3

import pytest

from clisweave import cb

CLI_SID = "01a0f6ee-6ad1-7f29-83cd-8524990d2131"
CID = "759c8f973e1044bba0bca7025a9af7da"
DESK = "dfa8aca9e038437e8b14f4357c534819"

T0 = 1790849237759  # ms
MIN = 60 * 1000


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Every store points at nothing until a test builds one, so nothing here
    ever reads the machine's real CodeBuddy data or its listing cache."""
    nowhere = tmp_path / "nowhere"
    monkeypatch.setattr(cb, "CLI_ROOT", str(nowhere / "projects"))
    monkeypatch.setattr(cb, "VS_GENIE", str(nowhere / "genie"))
    monkeypatch.setattr(cb, "VS_QUEUE", str(nowhere / "queue"))
    monkeypatch.setattr(cb, "VS_TODOS", str(nowhere / "todos"))
    monkeypatch.setattr(cb, "VS_CHANGES", str(nowhere / "changes"))
    monkeypatch.setattr(cb, "DESKTOP_DB", str(nowhere / "sessions.vscdb"))
    monkeypatch.setattr(cb, "LIST_CACHE_FILE", str(tmp_path / "cache" / "cb_last_list.json"))


# ---------- store builders ----------

def msg(role, text, ts=T0, cwd="/w/proj", sid=CLI_SID, kind="message"):
    return {"id": "m%d" % ts, "timestamp": ts, "type": kind, "role": role,
            "content": [{"type": "input_text", "text": text}],
            "sessionId": sid, "cwd": cwd}


def cli_store(tmp_path, slug, name, lines, raw=False):
    d = tmp_path / "cli" / slug
    d.mkdir(parents=True, exist_ok=True)
    body = lines if raw else "".join(json.dumps(x) + "\n" for x in lines)
    (d / name).write_text(body, encoding="utf-8")
    cb.CLI_ROOT = str(tmp_path / "cli")
    return d / name


def enc_ws(path):
    return base64.b64encode(path.encode()).decode().replace("=", "_")


def vscode_store(tmp_path, workspace, cid, updated=T0, items=0, todos=None, changed=0,
                 queue_name=None):
    root = tmp_path / "vs"
    (root / "genie" / enc_ws(workspace) / "conversations" / cid).mkdir(parents=True, exist_ok=True)
    q = root / "queue"
    q.mkdir(parents=True, exist_ok=True)
    (q / (queue_name or cid[:8] + ".json")).write_text(json.dumps({
        "version": 2, "lastUpdated": updated,
        "conversations": {cid: {"conversationId": cid, "updatedAt": updated,
                                "items": [{}] * items, "runtime": {}}}}))
    if todos:
        t = root / "todos"
        t.mkdir(exist_ok=True)
        (t / (cid + ".json")).write_text(json.dumps(
            {"conversationId": cid, "todos": [{"content": c} for c in todos]}))
    if changed:
        c = root / "changes" / cid
        c.mkdir(parents=True, exist_ok=True)
        for i in range(changed):
            (c / ("f%d.json" % i)).write_text("{}")
    cb.VS_GENIE, cb.VS_QUEUE = str(root / "genie"), str(q)
    cb.VS_TODOS, cb.VS_CHANGES = str(root / "todos"), str(root / "changes")


def desktop_store(tmp_path, sessions, extra_rows=()):
    db = tmp_path / "desktop.vscdb"
    con = sqlite3.connect(str(db))
    con.execute("create table ItemTable (key text unique on conflict replace, value blob)")
    for s in sessions:
        con.execute("insert into ItemTable values (?, ?)",
                    ("session:" + s["conversationId"], json.dumps(s)))
    for k, v in extra_rows:
        con.execute("insert into ItemTable values (?, ?)", (k, v))
    con.commit()
    con.close()
    cb.DESKTOP_DB = str(db)


def dsession(cid=DESK, cwd="/w/proj", title="Desk title", created=T0, updated=T0 + 5 * MIN):
    return {"conversationId": cid, "cwd": cwd, "title": title, "status": "Completed",
            "createdAt": created, "updatedAt": updated, "userId": "u"}


# ---------- cli ----------

def test_cli_session_reads_id_cwd_turns_and_span(tmp_path):
    cli_store(tmp_path, "w-proj", CLI_SID + ".jsonl", [
        msg("user", "first question", ts=T0),
        msg("assistant", "an answer", ts=T0 + MIN),
        msg("user", "second question", ts=T0 + 2 * MIN),
    ])
    [row] = cb.collect_cli()
    assert row["client"] == "cli" and row["id"] == CLI_SID and row["cwd"] == "/w/proj"
    assert row["turns"] == 2
    assert (row["created"], row["updated"]) == (T0, T0 + 2 * MIN)
    assert row["title"] == "first question"


def test_cli_ai_title_beats_the_first_prompt(tmp_path):
    cli_store(tmp_path, "w-proj", "a.jsonl", [
        msg("user", "hi"),
        {"type": "ai-title", "aiTitle": "Refactor the login flow", "timestamp": T0 + 1,
         "sessionId": CLI_SID, "cwd": "/w/proj"},
    ])
    [row] = cb.collect_cli()
    assert row["title"] == "Refactor the login flow" and row["title_source"] == "ai-title"


def test_cli_title_skips_injected_scaffolding(tmp_path):
    cli_store(tmp_path, "w-proj", "a.jsonl", [
        msg("user", '<system-reminder data-role="x">Caveat: ignore</system-reminder>fix the bug'),
    ])
    assert cb.collect_cli()[0]["title"] == "fix the bug"


def test_cli_session_of_only_command_output_says_so_not_empty(tmp_path):
    """Both prompts were a command's output fed back in: the session has turns,
    so labelling it '(no messages)' would be false."""
    cli_store(tmp_path, "w-proj", "a.jsonl", [
        msg("user", '<system-reminder data-role="command-caveat">Caveat</system-reminder>'),
        msg("user", "<local-command-stdout></local-command-stdout>", ts=T0 + 1),
    ])
    [row] = cb.collect_cli()
    assert row["turns"] == 2 and "command output only" in row["title"]


def test_cli_session_with_no_prompts_is_called_empty(tmp_path):
    cli_store(tmp_path, "w-proj", "a.jsonl", [msg("assistant", "hello")])
    assert cb.collect_cli()[0]["title"] == "(no messages)"


def test_cli_falls_back_to_the_filename_and_mtime_for_a_bare_file(tmp_path):
    p = cli_store(tmp_path, "w-proj", "abc-123.jsonl", [{"type": "message", "role": "user"}])
    [row] = cb.collect_cli()
    assert row["id"] == "abc-123"
    assert row["updated"] == int(p.stat().st_mtime * 1000)


def test_cli_tolerates_corrupt_and_non_object_lines(tmp_path):
    cli_store(tmp_path, "w-proj", "a.jsonl",
              "not json\n[1, 2]\n" + json.dumps(msg("user", "survivor")) + "\n\n", raw=True)
    [row] = cb.collect_cli()
    assert row["title"] == "survivor"


def test_cli_ignores_a_non_numeric_timestamp(tmp_path):
    cli_store(tmp_path, "w-proj", "a.jsonl", [
        dict(msg("user", "hi"), timestamp="yesterday"), msg("user", "again", ts=T0)])
    assert cb.collect_cli()[0]["created"] == T0


def test_cli_missing_store_is_just_empty():
    assert cb.collect_cli() == []


# ---------- vscode ----------

@pytest.mark.parametrize("path", ["/", "c:/Users/huntl/work/clisweave", "/home/lh/work/MC-DAQ-master",
                                  "/a", "/ab", "/abc"])
def test_workspace_dir_names_round_trip(path):
    assert cb.decode_workspace(enc_ws(path)) == path


def test_a_name_that_is_not_base64_decodes_to_none():
    assert cb.decode_workspace("not base64!") is None


def test_vscode_session_gets_cwd_from_its_workspace_dir(tmp_path):
    vscode_store(tmp_path, "c:/Users/huntl/work/clisweave", CID, updated=T0, items=0,
                 todos=["Run test suite", "Fix it"], changed=2)
    [row] = cb.collect_vscode()
    assert row["client"] == "vscode" and row["id"] == CID
    assert row["cwd"] == "c:/Users/huntl/work/clisweave"
    assert row["updated"] == T0
    assert row["title"] == "Run test suite" and row["title_source"] == "todo-hint"
    assert "no messages persisted" in row["note"]
    assert "2 todos" in row["note"] and "2 file snapshots" in row["note"]


def test_vscode_session_without_todos_is_untitled(tmp_path):
    vscode_store(tmp_path, "/w/proj", CID)
    assert "no title stored" in cb.collect_vscode()[0]["title"]


def test_vscode_turns_come_from_the_queue_items(tmp_path):
    vscode_store(tmp_path, "/w/proj", CID, items=3)
    row = cb.collect_vscode()[0]
    assert row["turns"] == 3 and "no messages persisted" not in row["note"]


def test_vscode_conversation_known_only_to_the_queue_has_no_cwd(tmp_path):
    vscode_store(tmp_path, "/w/proj", CID)
    other = "f" * 32
    (tmp_path / "vs" / "queue" / "other.json").write_text(json.dumps(
        {"conversations": {other: {"updatedAt": T0 + 9, "items": []}}}))
    rows = {r["id"]: r for r in cb.collect_vscode()}
    assert rows[other]["cwd"] is None and rows[other]["updated"] == T0 + 9


@pytest.mark.parametrize("body", ["{not json", "[]", '{"conversations": []}',
                                  '{"conversations": {"x": 5}}'])
def test_vscode_survives_a_malformed_queue_file(tmp_path, body):
    vscode_store(tmp_path, "/w/proj", CID)
    (tmp_path / "vs" / "queue" / "zz.json").write_text(body)
    assert [r["id"] for r in cb.collect_vscode()] == [CID]


def test_vscode_survives_malformed_todos(tmp_path):
    vscode_store(tmp_path, "/w/proj", CID)
    (tmp_path / "vs" / "todos").mkdir()
    (tmp_path / "vs" / "todos" / (CID + ".json")).write_text('{"todos": "nope"}')
    assert "no title stored" in cb.collect_vscode()[0]["title"]


def test_vscode_missing_store_is_just_empty():
    assert cb.collect_vscode() == []


# ---------- desktop ----------

def test_desktop_rows_come_from_session_keys_only(tmp_path):
    desktop_store(tmp_path, [dsession()], extra_rows=[("unrelated:key", "{}"), ("session:bad", "{")])
    [row] = cb.collect_desktop()
    assert row["client"] == "desktop" and row["id"] == DESK
    assert row["title"] == "Desk title" and row["cwd"] == "/w/proj" and row["note"] == "Completed"
    assert row["turns"] is None
    assert (row["created"], row["updated"]) == (T0, T0 + 5 * MIN)


def test_desktop_untitled_session(tmp_path):
    desktop_store(tmp_path, [dsession(title="  ")])
    assert cb.collect_desktop()[0]["title"] == "(no title)"


def test_desktop_missing_or_corrupt_db_is_just_empty(tmp_path):
    assert cb.collect_desktop() == []
    bad = tmp_path / "bad.vscdb"
    bad.write_text("this is not sqlite")
    cb.DESKTOP_DB = str(bad)
    assert cb.collect_desktop() == []


def test_desktop_db_without_the_table_is_just_empty(tmp_path):
    db = tmp_path / "empty.vscdb"
    sqlite3.connect(str(db)).close()
    cb.DESKTOP_DB = str(db)
    assert cb.collect_desktop() == []


# ---------- the unified listing ----------

def three_clients(tmp_path):
    cli_store(tmp_path, "w-proj", CLI_SID + ".jsonl", [msg("user", "cli prompt", ts=T0)])
    vscode_store(tmp_path, "/w/other", CID, updated=T0 + 10 * MIN)
    desktop_store(tmp_path, [dsession(cwd="/w/proj", updated=T0 + 5 * MIN)])


def test_listing_merges_all_three_clients_newest_first(tmp_path, capsys):
    three_clients(tmp_path)
    assert cb.main([]) == 0
    out = capsys.readouterr().out
    assert out.index("vscode") < out.index("desktop") < out.index("cli  ")
    assert "3 session(s): cli=1, vscode=1, desktop=1" in out


def test_listing_filters_by_client(tmp_path, capsys):
    three_clients(tmp_path)
    cb.main(["--client", "desktop"])
    out = capsys.readouterr().out
    assert "1 session(s): desktop=1" in out and "Desk title" in out


def test_listing_filters_by_cwd_prefix_ignoring_separators_and_case(tmp_path, capsys):
    cli_store(tmp_path, "s", "a.jsonl", [msg("user", "x", cwd="C:\\Users\\Hunt\\Proj")])
    vscode_store(tmp_path, "/elsewhere", CID)
    cb.main(["--cwd", "c:/users/hunt"])
    assert "1 session(s): cli=1" in capsys.readouterr().out


def test_listing_limit_and_all(tmp_path, capsys):
    three_clients(tmp_path)
    cb.main(["--limit", "2"])
    assert "2 session(s)" in capsys.readouterr().out
    cb.main(["--limit", "all"])
    assert "3 session(s)" in capsys.readouterr().out
    cb.main(["--limit", "junk"])  # unparseable falls back rather than crashing
    assert "3 session(s)" in capsys.readouterr().out


def test_full_is_sessions_without_a_cutoff(tmp_path, capsys):
    for i in range(25):
        cli_store(tmp_path, "s", "s%d.jsonl" % i, [msg("user", "p%d" % i, ts=T0 + i)])
    cb.main([])
    assert "20 session(s)" in capsys.readouterr().out
    cb.main(["full"])
    assert "25 session(s)" in capsys.readouterr().out


def test_listing_sort_by_cwd(tmp_path, capsys):
    cli_store(tmp_path, "s", "a.jsonl", [msg("user", "bbb", cwd="/b")])
    cli_store(tmp_path, "s", "c.jsonl", [msg("user", "aaa", cwd="/a")])
    cb.main(["--sort", "cwd"])
    out = capsys.readouterr().out
    assert out.index("aaa") < out.index("bbb")


def test_listing_json_is_machine_readable(tmp_path, capsys):
    three_clients(tmp_path)
    cb.main(["--json"])
    rows = json.loads(capsys.readouterr().out)
    assert {r["client"] for r in rows} == {"cli", "vscode", "desktop"}


def test_an_empty_machine_says_so(capsys):
    assert cb.main([]) == 0
    assert "(no sessions)" in capsys.readouterr().out


def test_full_ids_when_asked(tmp_path, capsys):
    three_clients(tmp_path)
    cb.main(["--full-id", "--client", "cli"])
    assert CLI_SID in capsys.readouterr().out


def test_a_title_the_console_cannot_show_does_not_abort_the_listing(tmp_path, capsys):
    cli_store(tmp_path, "s", "a.jsonl", [msg("user", "tune the \u2022 engine")])
    assert cb.main([]) == 0


# ---------- cross-client clusters ----------

def rows_for_clusters(tmp_path):
    cli_store(tmp_path, "s", "a.jsonl", [msg("user", "c", ts=T0 + 40 * MIN, cwd="/w/proj")])
    vscode_store(tmp_path, "/w/proj", CID, updated=T0 + 5 * MIN)
    desktop_store(tmp_path, [dsession(cwd="/w/proj", updated=T0)])


def test_nearby_rows_of_different_clients_in_one_directory_cluster(tmp_path):
    rows_for_clusters(tmp_path)
    [c] = [c for c in cb.cluster_rows(cb.collect(), 120) if len(c["rows"]) > 1]
    assert c["clients"] == {"cli", "vscode", "desktop"}


def test_the_window_splits_clusters(tmp_path):
    rows_for_clusters(tmp_path)
    multi = [c for c in cb.cluster_rows(cb.collect(), 10) if len(c["rows"]) > 1]
    assert [sorted(c["clients"]) for c in multi] == [["desktop", "vscode"]]


def test_one_client_never_holds_a_cluster_twice(tmp_path):
    """Two CLI sessions a minute apart are two sessions, not one conversation."""
    cli_store(tmp_path, "s", "a.jsonl", [msg("user", "one", ts=T0, sid="a")])
    cli_store(tmp_path, "s", "b.jsonl", [msg("user", "two", ts=T0 + MIN, sid="b")])
    clusters = cb.cluster_rows(cb.collect(), 120)
    assert len(clusters) == 2 and all(len(c["rows"]) == 1 for c in clusters)


def test_rows_in_different_directories_never_cluster(tmp_path):
    cli_store(tmp_path, "s", "a.jsonl", [msg("user", "x", cwd="/a")])
    desktop_store(tmp_path, [dsession(cwd="/b", updated=T0)])
    assert all(len(c["rows"]) == 1 for c in cb.cluster_rows(cb.collect(), 120))


def test_rows_with_no_directory_are_left_out_of_clusters(tmp_path):
    vscode_store(tmp_path, "/w/proj", CID)
    (tmp_path / "vs" / "queue" / "o.json").write_text(json.dumps(
        {"conversations": {"f" * 32: {"updatedAt": T0, "items": []}}}))
    assert sum(len(c["rows"]) for c in cb.cluster_rows(cb.collect(), 120)) == 1


def test_cluster_view_renders_and_does_not_replace_the_numbered_cache(tmp_path, capsys):
    rows_for_clusters(tmp_path)
    cb.main(["--limit", "all"])
    before = cb.read_list_cache()
    capsys.readouterr()
    cb.main(["--cluster"])
    out = capsys.readouterr().out
    assert "3 client(s): cli/desktop/vscode" in out
    assert cb.read_list_cache() == before


def test_cluster_view_json(tmp_path, capsys):
    rows_for_clusters(tmp_path)
    cb.main(["--cluster", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data[0]["clients"] and data[0]["sessions"]


def test_cluster_view_with_nothing_to_show(capsys):
    cb.main(["--cluster"])
    assert "(no sessions)" in capsys.readouterr().out


# ---------- resolving a target ----------

def test_a_row_number_means_a_row_of_the_last_listing(tmp_path):
    three_clients(tmp_path)
    cb.main([])
    listed = cb.read_list_cache()
    row, err = cb.resolve("2")
    assert err is None and row == listed[1]


def test_the_number_follows_the_listing_you_saw_not_a_fresh_scan(tmp_path):
    """A session that appears after the listing must not shift row 1."""
    three_clients(tmp_path)
    cb.main([])
    first = cb.read_list_cache()[0]
    cli_store(tmp_path, "s", "new.jsonl", [msg("user", "brand new", ts=T0 + 999 * MIN)])
    assert cb.resolve("1")[0] == first


def test_a_row_number_before_any_listing_asks_for_one():
    row, err = cb.resolve("1")
    assert row is None and "run `cb` first" in err


def test_a_row_number_out_of_range(tmp_path):
    three_clients(tmp_path)
    cb.main([])
    assert "out of range" in cb.resolve("9")[1]
    assert "out of range" in cb.resolve("0")[1]


def test_an_id_prefix_resolves_against_everything_on_disk(tmp_path):
    three_clients(tmp_path)
    assert cb.resolve("cli:01a0f6ee")[0]["id"] == CLI_SID
    assert cb.resolve("759c")[0]["id"] == CID
    assert cb.resolve("desktop:dfa8")[0]["id"] == DESK


def test_an_ambiguous_prefix_lists_candidates_instead_of_guessing(tmp_path):
    cli_store(tmp_path, "s", "a.jsonl", [msg("user", "x", sid="01a0aaaa")])
    cli_store(tmp_path, "s", "b.jsonl", [msg("user", "y", sid="01a0bbbb")])
    row, err = cb.resolve("cli:01a0")
    assert row is None and "matches 2 sessions" in err and "give more of the id" in err


def test_an_ambiguity_across_clients_says_to_name_the_client(tmp_path):
    cli_store(tmp_path, "s", "a.jsonl", [msg("user", "x", sid="abc111")])
    desktop_store(tmp_path, [dsession(cid="abc222")])
    assert "`<client>:`" in cb.resolve("abc")[1]


def test_no_match_and_unknown_client(tmp_path):
    three_clients(tmp_path)
    assert "no session matches" in cb.resolve("zzzz")[1]
    assert "unknown client" in cb.resolve("bogus:1")[1]


def test_a_corrupt_cache_reads_as_no_listing(tmp_path):
    import os
    os.makedirs(os.path.dirname(cb.LIST_CACHE_FILE), exist_ok=True)
    for body in ("{broken", "{}", '[{"client": "cli"}]', '[{"client": "nope", "id": "x"}]', "[1]"):
        with open(cb.LIST_CACHE_FILE, "w") as fh:
            fh.write(body)
        assert cb.read_list_cache() == []


# ---------- cb resume ----------

@pytest.fixture
def real_dir(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return str(d)


@pytest.fixture
def fake_tools(monkeypatch, tmp_path):
    """Pretend codebuddy, code and the desktop exe are installed, and record
    what resume would have run instead of running it."""
    exe = tmp_path / "CodeBuddy.exe"
    exe.write_text("")
    paths = {"codebuddy.cmd": "/bin/codebuddy.cmd", "code": "/bin/code"}
    monkeypatch.setattr(cb.shutil, "which", lambda n: paths.get(n))
    monkeypatch.setattr(cb, "_reg_protocol_exe", lambda: str(exe))
    ran = {"call": [], "popen": []}
    monkeypatch.setattr(cb.subprocess, "call", lambda cmd, cwd=None: ran["call"].append((cmd, cwd)) or 0)
    monkeypatch.setattr(cb.subprocess, "Popen", lambda cmd: ran["popen"].append(cmd))
    return ran


def listing_with_cli(tmp_path, real_dir):
    cli_store(tmp_path, "s", "a.jsonl", [msg("user", "resume me", cwd=real_dir)])
    cb.main([])


def test_resume_runs_the_cli_with_r_in_the_sessions_own_directory(tmp_path, real_dir, fake_tools, capsys):
    listing_with_cli(tmp_path, real_dir)
    assert cb.main(["resume", "1"]) == 0
    assert fake_tools["call"] == [(["/bin/codebuddy.cmd", "-r", CLI_SID], real_dir)]


def test_resume_dry_run_touches_nothing(tmp_path, real_dir, fake_tools, capsys):
    listing_with_cli(tmp_path, real_dir)
    capsys.readouterr()
    assert cb.main(["resume", "1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "-r " + CLI_SID in out and real_dir in out
    assert fake_tools == {"call": [], "popen": []}


def test_resume_passes_the_clis_exit_code_through(tmp_path, real_dir, monkeypatch, fake_tools):
    listing_with_cli(tmp_path, real_dir)
    monkeypatch.setattr(cb.subprocess, "call", lambda cmd, cwd=None: 7)
    assert cb.main(["resume", "1"]) == 7


def test_resume_reports_a_cli_that_cannot_be_spawned(tmp_path, real_dir, monkeypatch, fake_tools, capsys):
    listing_with_cli(tmp_path, real_dir)

    def boom(cmd, cwd=None):
        raise FileNotFoundError(2, "No such file", cmd[0])
    monkeypatch.setattr(cb.subprocess, "call", boom)
    assert cb.main(["resume", "1"]) == 127
    assert "could not run" in capsys.readouterr().err


def test_resume_ctrl_c_is_quiet(tmp_path, real_dir, monkeypatch, fake_tools):
    listing_with_cli(tmp_path, real_dir)

    def interrupted(cmd, cwd=None):
        raise KeyboardInterrupt
    monkeypatch.setattr(cb.subprocess, "call", interrupted)
    assert cb.main(["resume", "1"]) == 130


def test_resume_without_the_cli_installed_says_so(tmp_path, real_dir, monkeypatch, capsys):
    monkeypatch.setattr(cb.shutil, "which", lambda n: None)
    listing_with_cli(tmp_path, real_dir)
    assert cb.main(["resume", "1"]) == 1
    assert "no codebuddy executable" in capsys.readouterr().err


def test_resume_refuses_when_the_sessions_directory_is_gone(tmp_path, fake_tools, capsys):
    cli_store(tmp_path, "s", "a.jsonl", [msg("user", "x", cwd=str(tmp_path / "deleted"))])
    cb.main([])
    assert cb.main(["resume", "1"]) == 1
    assert fake_tools["call"] == []
    assert "pass --cwd" in capsys.readouterr().err


def test_resume_cwd_overrides_a_missing_directory(tmp_path, real_dir, fake_tools):
    cli_store(tmp_path, "s", "a.jsonl", [msg("user", "x", cwd=str(tmp_path / "deleted"))])
    cb.main([])
    assert cb.main(["resume", "1", "--cwd", real_dir]) == 0
    assert fake_tools["call"][0][1] == real_dir


def test_a_bare_row_number_is_shorthand_for_resume(tmp_path, real_dir, fake_tools):
    """`cw <N>` already means `cw resume <N>`; `cb` must match it."""
    listing_with_cli(tmp_path, real_dir)
    assert cb.main(["1"]) == 0
    assert fake_tools["call"] == [(["/bin/codebuddy.cmd", "-r", CLI_SID], real_dir)]


def test_the_shorthand_still_takes_resume_flags(tmp_path, real_dir, fake_tools):
    listing_with_cli(tmp_path, real_dir)
    assert cb.main(["1", "--dry-run"]) == 0
    assert fake_tools == {"call": [], "popen": []}


def test_resume_never_pretends_a_transcriptless_row_can_be_resumed_in_the_cli(tmp_path, real_dir, fake_tools, capsys):
    vscode_store(tmp_path, real_dir, CID)
    cb.main([])
    assert cb.main(["resume", "1", "--to", "cli"]) == 1
    assert fake_tools == {"call": [], "popen": []}
    assert "no local transcript" in capsys.readouterr().err


def test_resume_of_a_vscode_row_opens_the_workspace_and_says_what_it_cannot_do(tmp_path, real_dir, fake_tools, capsys):
    vscode_store(tmp_path, real_dir, CID)
    cb.main([])
    capsys.readouterr()
    assert cb.main(["resume", "1"]) == 0
    assert fake_tools["popen"] == [["/bin/code", real_dir]]
    assert "no command that opens one conversation" in capsys.readouterr().out


def test_resume_history_flag_brings_up_the_history_panel(tmp_path, real_dir, fake_tools):
    vscode_store(tmp_path, real_dir, CID)
    cb.main([])
    cb.main(["resume", "1", "--history"])
    assert fake_tools["popen"] == [["/bin/code", real_dir, "--command", cb.VSCODE_HISTORY_CMD]]


def test_resume_of_a_desktop_row_launches_the_app_on_the_folder(tmp_path, real_dir, fake_tools, capsys):
    desktop_store(tmp_path, [dsession(cwd=real_dir)])
    cb.main([])
    capsys.readouterr()
    assert cb.main(["resume", "1"]) == 0
    assert fake_tools["popen"][0][1:] == [real_dir]
    assert "no route opens one desktop conversation" in capsys.readouterr().out


def test_a_row_can_be_opened_in_another_client(tmp_path, real_dir, fake_tools):
    desktop_store(tmp_path, [dsession(cwd=real_dir)])
    cb.main([])
    assert cb.main(["resume", "1", "--to", "vscode"]) == 0
    assert fake_tools["popen"] == [["/bin/code", real_dir]]


def test_resume_vscode_without_code_on_path(tmp_path, real_dir, monkeypatch, capsys):
    monkeypatch.setattr(cb.shutil, "which", lambda n: None)
    vscode_store(tmp_path, real_dir, CID)
    cb.main([])
    assert cb.main(["resume", "1"]) == 1
    assert "no `code` executable" in capsys.readouterr().err


def test_resume_desktop_without_the_app_found(tmp_path, real_dir, monkeypatch, capsys):
    monkeypatch.setattr(cb, "_reg_protocol_exe", lambda: None)
    monkeypatch.setattr(cb.shutil, "which", lambda n: None)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    desktop_store(tmp_path, [dsession(cwd=real_dir)])
    cb.main([])
    assert cb.main(["resume", "1"]) == 1
    assert "desktop executable not found" in capsys.readouterr().err


def test_resume_an_error_goes_to_stderr_with_a_failing_code(tmp_path, capsys):
    assert cb.main(["resume", "5"]) == 1
    assert "run `cb` first" in capsys.readouterr().err


# ---------- command line ----------

def test_help_and_unknown_command(capsys):
    assert cb.main(["--help"]) == 0
    assert "cb resume" in capsys.readouterr().out
    assert cb.main(["bogus"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_default_command_is_the_listing_even_with_only_options(tmp_path, capsys):
    three_clients(tmp_path)
    assert cb.main(["--client", "cli"]) == 0
    assert "1 session(s): cli=1" in capsys.readouterr().out


def test_cb_does_not_touch_ais_list_cache(tmp_path):
    from clisweave import sessions
    assert cb.LIST_CACHE_FILE != sessions.LIST_CACHE_FILE
