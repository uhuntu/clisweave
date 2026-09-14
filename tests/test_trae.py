"""Coverage for the trae (Trae IDE) session source.

Trae is not a CLI: it stores sessions per-workspace in a state.vscdb SQLite
file under <install>/User/workspaceStorage/<hash>/, with workspace.json mapping
the hash back to a real directory. See sessions.trae_light_records.
"""
import json
import sqlite3

from clisweave import sessions


def _payload(text, saved_at=1_700_000_000_000):
    return {"savedAt": saved_at, "response": {"result": [{"text": text}]}}


def _make_workspace(base, ws_hash="ws1", folder="file:///C:/proj", rows=()):
    """Build a minimal Trae install tree under `base`, returning the workspace
    dir. `rows` are (key, value-dict) pairs written to the vscdb ItemTable."""
    ws_dir = base / "User" / "workspaceStorage" / ws_hash
    ws_dir.mkdir(parents=True)
    if folder is not None:
        (ws_dir / "workspace.json").write_text(json.dumps({"folder": folder}))
    conn = sqlite3.connect(str(ws_dir / "state.vscdb"))
    cur = conn.cursor()
    cur.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    for key, value in rows:
        cur.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)", (key, json.dumps(value)))
    conn.commit()
    conn.close()
    return ws_dir


def test_trae_light_records_reads_sessions_from_workspace_db(monkeypatch, tmp_path):
    _make_workspace(tmp_path, rows=[("ai-chat.chatQueryCompletion.v2.abc123", _payload("fix the parser"))])
    monkeypatch.setattr(sessions, "_trae_base_dirs", lambda: [str(tmp_path)])

    records = sessions.trae_light_records()

    assert len(records) == 1
    assert records[0]["tool"] == "trae"
    assert records[0]["id"] == "abc123"
    assert records[0]["ts"] == 1_700_000_000  # savedAt is milliseconds
    assert records[0]["cwd"] == "C:\\proj"
    assert records[0]["snippet"] == "fix the parser"


def test_trae_light_records_dedupes_same_session_across_install_variants(monkeypatch, tmp_path):
    """Trae CN / TRAE SOLO CN / TRAE SOLO are separate install dirs that can
    hold the same session id -- it must only be listed once."""
    base_a = tmp_path / "a"
    base_b = tmp_path / "b"
    for base in (base_a, base_b):
        _make_workspace(base, rows=[("ai-chat.chatQueryCompletion.v2.abc123", _payload("x"))])
    monkeypatch.setattr(sessions, "_trae_base_dirs", lambda: [str(base_a), str(base_b)])

    assert [r["id"] for r in sessions.trae_light_records()] == ["abc123"]


def test_trae_light_records_without_workspace_json_has_no_cwd(monkeypatch, tmp_path):
    _make_workspace(tmp_path, folder=None, rows=[("ai-chat.chatQueryCompletion.v2.abc123", _payload("x"))])
    monkeypatch.setattr(sessions, "_trae_base_dirs", lambda: [str(tmp_path)])

    assert sessions.trae_light_records()[0]["cwd"] is None


def test_trae_title_uses_first_result_text(monkeypatch, tmp_path):
    _make_workspace(tmp_path, rows=[("ai-chat.chatQueryCompletion.v2.abc123", _payload("fix the parser"))])
    monkeypatch.setattr(sessions, "_trae_base_dirs", lambda: [str(tmp_path)])

    assert sessions.trae_title(sessions.trae_light_records()[0]) == "fix the parser"


def test_trae_title_falls_back_when_no_text(monkeypatch, tmp_path):
    _make_workspace(tmp_path, rows=[("ai-chat.chatQueryCompletion.v2.abc123", {"savedAt": 1})])
    monkeypatch.setattr(sessions, "_trae_base_dirs", lambda: [str(tmp_path)])

    assert sessions.trae_title(sessions.trae_light_records()[0]) == "(no title)"


def test_trae_resolve_and_session_cwd(monkeypatch, tmp_path):
    _make_workspace(tmp_path, rows=[("ai-chat.chatQueryCompletion.v2.abc123", _payload("x"))])
    monkeypatch.setattr(sessions, "_trae_base_dirs", lambda: [str(tmp_path)])

    assert sessions.trae_resolve("abc") == ["abc123"]
    assert sessions.trae_session_cwd("abc123") == "C:\\proj"


def test_cmd_list_includes_trae_rows(monkeypatch, tmp_path, capsys):
    _make_workspace(tmp_path, rows=[("ai-chat.chatQueryCompletion.v2.abc123", _payload("fix the parser"))])
    monkeypatch.setattr(sessions, "_trae_base_dirs", lambda: [str(tmp_path)])
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", str(tmp_path / "no-claude"))
    monkeypatch.setattr(sessions, "CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr(sessions, "KIMI_HOME", str(tmp_path / "no-kimi"))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "cache" / "last_list.json"))

    sessions.cmd_list([])

    out = capsys.readouterr().out
    assert "trae" in out
    assert "fix the parser" in out
