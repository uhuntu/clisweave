import json
import os
import time

import pytest

from clisweave import sessions


@pytest.fixture(autouse=True)
def _reset_codex_path_cache():
    # codex_rollout_path() lazily caches sessions.CODEX_HOME's rollout file
    # listing in a module-level global; reset it around every test so one
    # test's tmp_path can't leak into another's.
    sessions._codex_path_index = None
    yield
    sessions._codex_path_index = None


@pytest.fixture(autouse=True)
def _isolate_cwd_overrides_file(monkeypatch, tmp_path):
    # Never let a test read or write the real user's cwd_overrides.json.
    monkeypatch.setattr(sessions, "CWD_OVERRIDES_FILE", str(tmp_path / "cwd_overrides.json"))


def write_codex_rollout(codex_home, sid, cwd=None, user_text=None, mtime=None):
    """Create a minimal codex rollout file, the actual on-disk source of
    truth codex_light_records() now scans directly (session_index.jsonl is
    only an optional title-enrichment source, not guaranteed to exist)."""
    day_dir = codex_home / "sessions" / "2026" / "08" / "14"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-2026-08-14T00-00-00-{sid}.jsonl"
    lines = [json.dumps({
        "timestamp": "2026-08-14T00:00:00.000Z", "type": "session_meta",
        "payload": {"id": sid, "cwd": cwd},
    })]
    if user_text is not None:
        lines.append(json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": user_text}],
            },
        }))
    path.write_text("\n".join(lines) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---------- relative_time ----------

@pytest.mark.parametrize(
    "delta,expected",
    [
        (0, "0s ago"),
        (30, "30s ago"),
        (90, "1m ago"),
        (3661, "1h ago"),
        (90000, "1d ago"),
    ],
)
def test_relative_time(monkeypatch, delta, expected):
    now = 1_800_000_000
    monkeypatch.setattr(sessions.time, "time", lambda: now)
    assert sessions.relative_time(now - delta) == expected


def test_relative_time_missing_ts():
    assert sessions.relative_time(0) == "?"
    assert sessions.relative_time(None) == "?"


# ---------- codex ----------

def test_codex_thread_names_dedupes_reindexed_thread_rename_uses_utc(monkeypatch, tmp_path):
    """Regression test: codex appends a new session_index.jsonl line each
    time a thread gets auto-renamed, without removing the stale line for
    the same id, and updated_at is UTC ("...Z") -- timegm (not mktime, which
    would reinterpret it as local time) must be used so the *later* rename
    always wins regardless of local TZ."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "session_index.jsonl").write_text(
        json.dumps({
            "id": "019f5f6e-a0d0-71e0-9463-8158f339b400",
            "thread_name": "Understand current project",
            "updated_at": "2026-07-14T07:01:53.936145493Z",
        }) + "\n"
        + json.dumps({
            "id": "019f5f6e-a0d0-71e0-9463-8158f339b400",
            "thread_name": "Understand current project (2)",
            "updated_at": "2026-07-14T07:01:55.450675466Z",
        }) + "\n"
    )
    monkeypatch.setattr(sessions, "CODEX_HOME", str(codex_home))

    assert sessions.codex_thread_names() == {
        "019f5f6e-a0d0-71e0-9463-8158f339b400": "Understand current project (2)",
    }


def test_codex_light_records_finds_sessions_with_no_index_entry(monkeypatch, tmp_path):
    """Regression test: session_index.jsonl is only populated by the bare
    CLI's own terminal/exec-mode session tracking. Sessions created via
    other integrations (VSCode, Codex Desktop) use an entirely different
    session store and never get an entry there, even though their rollout
    file exists on disk same as any other session -- codex_light_records
    must still find them by scanning ~/.codex/sessions directly, not by
    relying on session_index.jsonl (which may not even exist)."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    # deliberately no session_index.jsonl at all
    sid = "019ffdbe-12ce-7e22-9a7f-30237f491124"
    write_codex_rollout(codex_home, sid, cwd="/data/hunt/work", user_text="fix the login crash")
    monkeypatch.setattr(sessions, "CODEX_HOME", str(codex_home))

    recs = sessions.codex_light_records()
    assert len(recs) == 1
    assert recs[0]["id"] == sid
    assert recs[0]["title"] is None  # no thread_name -- resolve_row falls back to codex_rollout_title
    assert recs[0]["ts"] > 0


def test_codex_light_records_prefers_index_title_when_available(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    sid = "019ffdbe-12ce-7e22-9a7f-30237f491124"
    write_codex_rollout(codex_home, sid, cwd="/data/hunt/work")
    (codex_home / "session_index.jsonl").write_text(json.dumps({
        "id": sid, "thread_name": "Fix login crash", "updated_at": "2026-08-14T00:00:00Z",
    }) + "\n")
    monkeypatch.setattr(sessions, "CODEX_HOME", str(codex_home))

    recs = sessions.codex_light_records()
    assert recs[0]["title"] == "Fix login crash"


def test_codex_rollout_title_skips_injected_boilerplate(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    sid = "019ffdbe-12ce-7e22-9a7f-30237f491124"
    day_dir = codex_home / "sessions" / "2026" / "08" / "14"
    day_dir.mkdir(parents=True)
    path = day_dir / f"rollout-2026-08-14T00-00-00-{sid}.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": "/x"}}) + "\n"
        + json.dumps({
            "type": "response_item",
            "payload": {"type": "message", "role": "developer",
                        "content": [{"type": "input_text", "text": "<permissions instructions>..."}]},
        }) + "\n"
        + json.dumps({
            "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /x\n..."}]},
        }) + "\n"
        + json.dumps({
            "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "<environment_context>\n  <cwd>/x</cwd>\n..."}]},
        }) + "\n"
        + json.dumps({
            "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "please fix the login crash"}]},
        }) + "\n"
    )
    monkeypatch.setattr(sessions, "CODEX_HOME", str(codex_home))

    assert sessions.codex_rollout_title(sid) == "please fix the login crash"


def test_codex_rollout_title_missing_session_returns_placeholder(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "CODEX_HOME", str(tmp_path / ".codex"))
    assert sessions.codex_rollout_title("no-such-id") == "(no title)"


def test_codex_rollout_title_does_not_treat_long_real_messages_as_boilerplate(monkeypatch, tmp_path):
    """Regression test: a real session had a genuine 1304-char task request
    (multiple bullet-pointed change requests) wrongly filtered out by a
    "skip if over 1000 chars" heuristic meant to catch AGENTS.md dumps,
    leaving the session with no title in either `ai search` or the plain
    listing even though it was a real, important conversation."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    sid = "01a018ff-5a11-7b2c-9d30-4f67e8a90123"
    long_message = "Hunt,\n\nThank you for the update. " + ("Please also fix this other thing. " * 30)
    assert len(long_message) > 1000
    write_codex_rollout(codex_home, sid, cwd="/data/hunt/work", user_text=long_message)
    monkeypatch.setattr(sessions, "CODEX_HOME", str(codex_home))

    title = sessions.codex_rollout_title(sid)
    assert title != "(no title)"
    assert title.startswith("Hunt,")


def test_codex_rollout_snippet_includes_assistant_text(monkeypatch, tmp_path):
    """Regression test: a real session about decompiling the SetupWizard
    APK on device MACH_MP had its entire substance in Codex's own
    responses -- the user only sent short directives and file-path
    fragments. codex_rollout_snippet must scan assistant output_text too,
    not just user input_text, or `ai search` misses genuinely on-topic
    sessions like this one entirely."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    sid = "01a037ea-949e-7a90-bd26-df49edc1585a"
    day_dir = codex_home / "sessions" / "2026" / "08" / "25"
    day_dir.mkdir(parents=True)
    path = day_dir / f"rollout-2026-08-25T15-55-15-{sid}.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": "/x"}}) + "\n"
        + json.dumps({
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "6 taps"}]},
        }) + "\n"
        + json.dumps({
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "I decompiled the SetupWizard APK from device MACH_MP into SetupWizard-MACH_MP-decompiled-bad"},
            ]},
        }) + "\n"
    )
    monkeypatch.setattr(sessions, "CODEX_HOME", str(codex_home))

    snippet = sessions.codex_rollout_snippet(sid)
    assert "SetupWizard-MACH_MP-decompiled-bad" in snippet

    # title stays user-only -- it's meant to reflect what was asked, and
    # shouldn't turn into a chunk of the assistant's response
    assert sessions.codex_rollout_title(sid) == "6 taps"


def test_codex_rollout_snippet_collects_multiple_messages(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    sid = "019ffdbe-12ce-7e22-9a7f-30237f491124"
    day_dir = codex_home / "sessions" / "2026" / "08" / "14"
    day_dir.mkdir(parents=True)
    path = day_dir / f"rollout-2026-08-14T00-00-00-{sid}.jsonl"

    def user_line(text):
        return json.dumps({
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
        })

    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": "/x"}}) + "\n"
        + user_line("change the default navigation bar mode") + "\n"
        + user_line("also update the webview") + "\n"
    )
    monkeypatch.setattr(sessions, "CODEX_HOME", str(codex_home))

    snippet = sessions.codex_rollout_snippet(sid)
    assert "navigation bar mode" in snippet
    assert "update the webview" in snippet


def test_codex_rollout_snippet_missing_session_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "CODEX_HOME", str(tmp_path / ".codex"))
    assert sessions.codex_rollout_snippet("no-such-id") == ""


def test_codex_resolve_finds_rollout_only_sessions(monkeypatch, tmp_path):
    """codex_resolve must find sessions that only exist as rollout files,
    not just ones present in session_index.jsonl (which may not exist)."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    sid = "019ffdbe-12ce-7e22-9a7f-30237f491124"
    write_codex_rollout(codex_home, sid, cwd="/x")
    monkeypatch.setattr(sessions, "CODEX_HOME", str(codex_home))

    assert sessions.codex_resolve("019ffdbe") == [sid]


def test_resolve_row_codex_falls_back_to_rollout_title_and_cwd(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    sid = "019ffdbe-12ce-7e22-9a7f-30237f491124"
    write_codex_rollout(codex_home, sid, cwd="/data/hunt/work", user_text="fix login crash")
    monkeypatch.setattr(sessions, "CODEX_HOME", str(codex_home))

    recs = sessions.codex_light_records()
    row = sessions.resolve_row(recs[0])
    assert row[5] == "fix login crash"  # title
    assert row[4] == "/data/hunt/work"  # cwd


def test_codex_resolve_prefix_match(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    index = codex_home / "session_index.jsonl"
    index.write_text(
        json.dumps({"id": "aaaa1111-0000-0000-0000-000000000000", "updated_at": "2026-01-01T00:00:00Z"}) + "\n"
        + json.dumps({"id": "aaaa2222-0000-0000-0000-000000000000", "updated_at": "2026-01-01T00:00:00Z"}) + "\n"
        + json.dumps({"id": "bbbb0000-0000-0000-0000-000000000000", "updated_at": "2026-01-01T00:00:00Z"}) + "\n"
    )
    monkeypatch.setattr(sessions, "CODEX_HOME", str(codex_home))

    assert sessions.codex_resolve("bbbb") == ["bbbb0000-0000-0000-0000-000000000000"]
    assert sessions.codex_resolve("aaaa") == [
        "aaaa1111-0000-0000-0000-000000000000",
        "aaaa2222-0000-0000-0000-000000000000",
    ]
    assert sessions.codex_resolve("zzzz") == []


# ---------- claude ----------

def test_claude_light_records_dedupes_same_session_across_project_dirs(monkeypatch, tmp_path):
    """Regression test: Claude Code stores a session's transcript under
    every project directory the session's cwd ever touched (e.g. via `cd`
    in tool calls), so the same session id can appear as multiple files.
    Only the most recently modified copy should be reported."""
    projects = tmp_path / "projects"
    dir_a = projects / "-home-hunt"
    dir_b = projects / "-home-hunt-work-aimux"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    sid = "cd385445-cec2-43c6-9919-69e87818d2dc"
    old_copy = dir_a / f"{sid}.jsonl"
    new_copy = dir_b / f"{sid}.jsonl"
    old_copy.write_text("{}\n")
    new_copy.write_text("{}\n")

    now = time.time()
    os.utime(old_copy, (now - 100, now - 100))
    os.utime(new_copy, (now, now))

    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", str(projects))

    recs = sessions.claude_light_records()
    assert len(recs) == 1
    assert recs[0]["path"] == str(new_copy)  # kept the more recently modified copy


def test_claude_title_and_cwd_prefers_real_cwd_over_dirname_guess(tmp_path):
    session_file = tmp_path / "abcd1234.jsonl"
    session_file.write_text(
        json.dumps({"type": "queue-operation", "content": "ignored"}) + "\n"
        + json.dumps({
            "type": "user",
            "cwd": "/home/hunt",
            "message": {"role": "user", "content": "hello there"},
        }) + "\n"
    )

    title, cwd = sessions.claude_title_and_cwd(str(session_file), cwd_fallback="/home/hunt/work/aimux")
    assert title == "hello there"
    assert cwd == "/home/hunt"  # real cwd wins over the directory-name guess


def test_claude_title_and_cwd_falls_back_when_no_cwd_field(tmp_path):
    session_file = tmp_path / "abcd1234.jsonl"
    session_file.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n"
    )
    title, cwd = sessions.claude_title_and_cwd(str(session_file), cwd_fallback="/guessed/path")
    assert title == "hi"
    assert cwd == "/guessed/path"


def test_claude_title_and_cwd_missing_file():
    title, cwd = sessions.claude_title_and_cwd("/no/such/file.jsonl", cwd_fallback="/fallback")
    assert title == "(no title)"
    assert cwd == "/fallback"


def test_claude_content_list_with_text_block(tmp_path):
    session_file = tmp_path / "s.jsonl"
    session_file.write_text(json.dumps({
        "type": "user",
        "message": {"content": [{"type": "image"}, {"type": "text", "text": "the real prompt"}]},
    }) + "\n")
    title, _ = sessions.claude_title_and_cwd(str(session_file), cwd_fallback=None)
    assert title == "the real prompt"


def test_sample_stride_covers_latter_middle_of_long_list():
    """Regression test: a real 104-message session had its relevant
    content at message 71 -- neither a first-N-only scan nor a first+last
    split reliably lands there (71 is past the first dozen, but well
    before the last dozen too). An even stride across the whole list
    should land near it."""
    texts = [str(i) for i in range(104)]
    sampled = sessions.sample_stride(texts, 12)
    assert any(abs(int(t) - 71) <= 4 for t in sampled)


def test_sample_stride_short_list_returns_everything():
    texts = ["a", "b", "c"]
    assert sessions.sample_stride(texts, 12) == texts


def test_sample_stride_title_sized_limit_keeps_plain_first_n():
    # limit <= 2 is used for title extraction, which wants literally the
    # first message(s) in order, not a stride blend
    texts = [str(i) for i in range(50)]
    assert sessions.sample_stride(texts, 1) == ["0"]


def test_join_with_fair_budget_gives_every_message_a_share():
    """Regression test: joining sampled texts then truncating the whole
    string let early, verbose messages consume the entire budget, silently
    dropping every later-sampled message -- the exact bug that made
    sample_stride's own fix ineffective on the real session until this was
    added (all 6 early messages combined already exceeded the char budget,
    so the 6 later ones, including the actually relevant one, never
    appeared in the final snippet)."""
    texts = ["x" * 500, "SetupWizard MACH_MP decompiled", "y" * 500]
    result = sessions.join_with_fair_budget(texts, max_chars=300)
    assert "SetupWizard" in result


def test_claude_snippet_includes_assistant_text(tmp_path):
    """Regression test: a real session's substance (decompiling an APK,
    the specific files/findings) was entirely in the assistant's own
    responses -- the user only gave short directives ("yes", "6 taps").
    A user-only scan found nothing, even though the session was exactly
    on topic; `ai search` reported no relevant sessions for a real match."""
    session_file = tmp_path / "s.jsonl"
    session_file.write_text(
        json.dumps({"type": "user", "message": {"content": "check it"}}) + "\n"
        + json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": "let me check"}]},
        }) + "\n"
        + json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "I decompiled the SetupWizard APK from device MACH_MP"}]},
        }) + "\n"
    )

    snippet = sessions.claude_snippet(str(session_file))
    assert "decompiled the SetupWizard APK" in snippet
    assert "let me check" not in snippet  # thinking blocks are not real response text


def test_claude_snippet_finds_topic_past_old_80_line_cutoff(tmp_path):
    """Regression test: a real session had its relevant message at line 92
    of 191, past the old 80-line/3-message scan window, so `ai search`
    never saw it and wrongly reported no relevant sessions."""
    session_file = tmp_path / "s.jsonl"
    # irrelevant event type, not "user"/"assistant" -- padding that both the
    # old and new scan logic correctly skip, isolating this test to the
    # scan-window fix rather than the separate assistant-text fix
    lines = [json.dumps({"type": "queue-operation", "content": "padding"}) for _ in range(90)]
    lines.append(json.dumps({
        "type": "user",
        "message": {"content": "please change the default navigation bar mode from taskbar back"},
    }))
    session_file.write_text("\n".join(lines) + "\n")

    snippet = sessions.claude_snippet(str(session_file))
    assert "navigation bar mode" in snippet


# ---------- kimi ----------

def test_kimi_resolve_tries_session_prefix_fallback(monkeypatch, tmp_path):
    kimi_home = tmp_path / ".kimi-code"
    kimi_home.mkdir()
    index = kimi_home / "session_index.jsonl"
    index.write_text(json.dumps({
        "sessionId": "session_97946bc7-c5d4-4419-85d1-1316cb7f4295",
        "sessionDir": str(tmp_path / "sessdir"),
    }) + "\n")
    monkeypatch.setattr(sessions, "KIMI_HOME", str(kimi_home))

    # bare prefix, without the "session_" the id actually starts with
    assert sessions.kimi_resolve("97946bc7") == ["session_97946bc7-c5d4-4419-85d1-1316cb7f4295"]
    # already-prefixed also works
    assert sessions.kimi_resolve("session_97946bc7") == ["session_97946bc7-c5d4-4419-85d1-1316cb7f4295"]


def test_kimi_snippet_includes_assistant_response_text(tmp_path):
    sess_dir = tmp_path / "sessdir"
    (sess_dir / "agents" / "main").mkdir(parents=True)
    wire = sess_dir / "agents" / "main" / "wire.jsonl"
    wire.write_text(
        json.dumps({"type": "turn.prompt", "input": [{"type": "text", "text": "check it"}]}) + "\n"
        + json.dumps({
            "type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {"type": "think", "think": "let me check"}},
        }) + "\n"
        + json.dumps({
            "type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {
                "type": "text", "text": "I decompiled the SetupWizard APK from device MACH_MP",
            }},
        }) + "\n"
    )

    snippet = sessions.kimi_snippet(str(sess_dir))
    assert "decompiled the SetupWizard APK" in snippet
    assert "let me check" not in snippet  # think parts are not real response text


def test_kimi_snippet_finds_topic_past_old_120_line_cutoff(tmp_path):
    sess_dir = tmp_path / "sessdir"
    (sess_dir / "agents" / "main").mkdir(parents=True)
    wire = sess_dir / "agents" / "main" / "wire.jsonl"
    lines = [json.dumps({"type": "llm.request"}) for _ in range(130)]
    lines.append(json.dumps({
        "type": "turn.prompt",
        "input": [{"type": "text", "text": "please change the default navigation bar mode"}],
    }))
    wire.write_text("\n".join(lines) + "\n")

    snippet = sessions.kimi_snippet(str(sess_dir))
    assert "navigation bar mode" in snippet


def test_kimi_light_records_skips_archived_unless_all(monkeypatch, tmp_path):
    kimi_home = tmp_path / ".kimi-code"
    kimi_home.mkdir()
    sess_dir = tmp_path / "sessdir"
    sess_dir.mkdir()
    (sess_dir / "state.json").write_text(json.dumps({
        "cwd": "/home/hunt", "updatedAt": 1700000000000, "archived": True,
    }))
    (kimi_home / "session_index.jsonl").write_text(json.dumps({
        "sessionId": "session_archived", "sessionDir": str(sess_dir),
    }) + "\n")
    monkeypatch.setattr(sessions, "KIMI_HOME", str(kimi_home))

    assert sessions.kimi_light_records(show_all=False) == []
    assert len(sessions.kimi_light_records(show_all=True)) == 1


# ---------- kimi: older state.json schema (ISO timestamps, workDir not cwd) ----------

def test_parse_kimi_timestamp_handles_epoch_ms_and_iso_string():
    assert sessions.parse_kimi_timestamp(1786944176340) == 1786944176340 / 1000.0
    assert sessions.parse_kimi_timestamp("2026-07-20T01:49:19.177Z") == 1784512159
    assert sessions.parse_kimi_timestamp(None) == 0
    assert sessions.parse_kimi_timestamp("") == 0
    assert sessions.parse_kimi_timestamp("not a date") == 0


def test_kimi_light_records_handles_older_schema(monkeypatch, tmp_path):
    """Regression test: older kimi-code sessions store updatedAt/createdAt
    as ISO-8601 strings (not epoch ms) and have no "cwd" key at all -- only
    "workDir". Both used to produce ts=0 / cwd=None ("?" in the listing)."""
    kimi_home = tmp_path / ".kimi-code"
    kimi_home.mkdir()
    sess_dir = tmp_path / "sessdir"
    sess_dir.mkdir()
    (sess_dir / "state.json").write_text(json.dumps({
        "createdAt": "2026-07-19T01:14:01.737Z",
        "updatedAt": "2026-07-20T01:49:19.177Z",
        "title": "hi",
        "workDir": "/data/ThunderBird",
    }))
    (kimi_home / "session_index.jsonl").write_text(json.dumps({
        "sessionId": "session_old", "sessionDir": str(sess_dir), "workDir": "/data/ThunderBird",
    }) + "\n")
    monkeypatch.setattr(sessions, "KIMI_HOME", str(kimi_home))

    recs = sessions.kimi_light_records(show_all=False)
    assert len(recs) == 1
    assert recs[0]["ts"] > 0
    assert recs[0]["cwd"] == "/data/ThunderBird"


def test_kimi_light_records_falls_back_to_index_workdir_when_state_has_neither(monkeypatch, tmp_path):
    kimi_home = tmp_path / ".kimi-code"
    kimi_home.mkdir()
    sess_dir = tmp_path / "sessdir"
    sess_dir.mkdir()
    (sess_dir / "state.json").write_text(json.dumps({"updatedAt": 1700000000000}))
    (kimi_home / "session_index.jsonl").write_text(json.dumps({
        "sessionId": "session_x", "sessionDir": str(sess_dir), "workDir": "/from/index",
    }) + "\n")
    monkeypatch.setattr(sessions, "KIMI_HOME", str(kimi_home))

    recs = sessions.kimi_light_records(show_all=False)
    assert recs[0]["cwd"] == "/from/index"


def test_kimi_session_cwd_reads_workdir_from_index(monkeypatch, tmp_path):
    kimi_home = tmp_path / ".kimi-code"
    kimi_home.mkdir()
    (kimi_home / "session_index.jsonl").write_text(
        json.dumps({"sessionId": "session_a", "sessionDir": "/x", "workDir": "/mnt/win/ThunderBird"}) + "\n"
        + json.dumps({"sessionId": "session_b", "sessionDir": "/y", "workDir": "/home/hunt"}) + "\n"
    )
    monkeypatch.setattr(sessions, "KIMI_HOME", str(kimi_home))

    assert sessions.kimi_session_cwd("session_a") == "/mnt/win/ThunderBird"
    assert sessions.kimi_session_cwd("session_b") == "/home/hunt"
    assert sessions.kimi_session_cwd("session_unknown") is None


def test_cmd_resume_kimi_chdirs_into_session_workdir_first(monkeypatch, tmp_path, capsys):
    """Regression test: `kimi -S <id>` refuses to resume a session created
    under a different cwd. Rather than surfacing that raw error, ai resume
    should chdir into the session's own recorded workDir first."""
    other_dir = tmp_path / "other-project"
    other_dir.mkdir()

    kimi_home = tmp_path / ".kimi-code"
    kimi_home.mkdir()
    (kimi_home / "session_index.jsonl").write_text(
        json.dumps({"sessionId": "session_abc", "sessionDir": "/x", "workDir": str(other_dir)}) + "\n"
    )
    monkeypatch.setattr(sessions, "KIMI_HOME", str(kimi_home))

    starting_dir = tmp_path
    monkeypatch.chdir(starting_dir)

    exec_calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: exec_calls.append(argv))

    sessions.cmd_resume(["kimi", "session_abc"])

    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(other_dir))
    assert exec_calls == [["kimi", "-S", "session_abc"]]
    assert "switching there first" in capsys.readouterr().err


def test_cmd_resume_kimi_skips_chdir_when_already_in_workdir(monkeypatch, tmp_path, capsys):
    kimi_home = tmp_path / ".kimi-code"
    kimi_home.mkdir()
    (kimi_home / "session_index.jsonl").write_text(
        json.dumps({"sessionId": "session_abc", "sessionDir": "/x", "workDir": str(tmp_path)}) + "\n"
    )
    monkeypatch.setattr(sessions, "KIMI_HOME", str(kimi_home))
    monkeypatch.chdir(tmp_path)

    exec_calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: exec_calls.append(argv))

    sessions.cmd_resume(["kimi", "session_abc"])

    assert exec_calls == [["kimi", "-S", "session_abc"]]
    assert "switching there first" not in capsys.readouterr().err


def test_cmd_resume_claude_chdirs_into_session_cwd_first(monkeypatch, tmp_path, capsys):
    other_dir = tmp_path / "other-project"
    other_dir.mkdir()

    projects = tmp_path / "projects"
    project_dir = projects / "-some-project"
    project_dir.mkdir(parents=True)
    sid = "cd385445-cec2-43c6-9919-69e87818d2dc"
    (project_dir / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": str(other_dir), "message": {"content": "hi"}}) + "\n"
    )
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", str(projects))
    monkeypatch.chdir(tmp_path)

    exec_calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: exec_calls.append(argv))

    sessions.cmd_resume(["claude", sid])

    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(other_dir))
    assert exec_calls == [["claude", "--resume", sid]]
    assert "switching there first" in capsys.readouterr().err


def test_cmd_resume_codex_chdirs_into_session_cwd_first(monkeypatch, tmp_path, capsys):
    other_dir = tmp_path / "other-project"
    other_dir.mkdir()

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    sid = "019ffdbe-12ce-7e22-9a7f-30237f491124"
    write_codex_rollout(codex_home, sid, cwd=str(other_dir))
    monkeypatch.setattr(sessions, "CODEX_HOME", str(codex_home))
    monkeypatch.chdir(tmp_path)

    exec_calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: exec_calls.append(argv))

    sessions.cmd_resume(["codex", sid])

    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(other_dir))
    assert exec_calls == [["codex", "resume", sid]]
    assert "switching there first" in capsys.readouterr().err


def test_cmd_resume_cwd_override_wins_over_session_original_dir(monkeypatch, tmp_path, capsys):
    """--cwd forces a directory even when it differs from the session's
    own recorded cwd (e.g. resuming a claude session into an unrelated
    project on purpose)."""
    session_original_dir = tmp_path / "original-project"
    session_original_dir.mkdir()
    forced_dir = tmp_path / "wrong-question-book"
    forced_dir.mkdir()

    projects = tmp_path / "projects"
    project_dir = projects / "-some-project"
    project_dir.mkdir(parents=True)
    sid = "cd385445-cec2-43c6-9919-69e87818d2dc"
    (project_dir / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": str(session_original_dir), "message": {"content": "hi"}}) + "\n"
    )
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", str(projects))
    monkeypatch.chdir(tmp_path)

    exec_calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: exec_calls.append(argv))

    sessions.cmd_resume(["claude", sid, "--cwd", str(forced_dir)])

    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(forced_dir))
    assert exec_calls == [["claude", "--resume", sid]]
    err = capsys.readouterr().err
    assert "forcing cwd" in err
    assert "switching there first" not in err


def test_cmd_resume_cwd_override_rejects_non_directory(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", str(tmp_path / "no-claude"))
    monkeypatch.setattr(sessions, "claude_resolve", lambda prefix: [prefix])

    with pytest.raises(SystemExit):
        sessions.cmd_resume(["claude", "some-id", "--cwd", str(tmp_path / "does-not-exist")])

    assert "is not a directory" in capsys.readouterr().err


def test_cmd_resume_cwd_override_flows_through_resume_by_number(monkeypatch, tmp_path, capsys):
    forced_dir = tmp_path / "wrong-question-book"
    forced_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(sessions, "read_list_cache", lambda: [{"tool": "codex", "id": "abc123"}])
    monkeypatch.setattr(sessions, "codex_resolve", lambda prefix: ["abc123"])
    monkeypatch.setattr(sessions, "codex_cwd", lambda sid: str(tmp_path / "unrelated"))

    exec_calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: exec_calls.append(argv))

    sessions.cmd_resume(["1", "--cwd", str(forced_dir)])

    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(forced_dir))
    assert exec_calls == [["codex", "resume", "abc123"]]


def test_cmd_resume_cwd_override_persists_and_is_reused_without_flag(monkeypatch, tmp_path, capsys):
    """--cwd should stick: a later plain `ai resume` (no --cwd) for the same
    session auto-switches to the pinned directory, and `ai sessions` shows
    it instead of the tool's own recorded cwd."""
    session_original_dir = tmp_path / "original-project"
    session_original_dir.mkdir()
    forced_dir = tmp_path / "wrong-question-book"
    forced_dir.mkdir()
    sid = "019ffdbe-12ce-7e22-9a7f-30237f491124"

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    write_codex_rollout(codex_home, sid, cwd=str(session_original_dir))
    monkeypatch.setattr(sessions, "CODEX_HOME", str(codex_home))

    exec_calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: exec_calls.append(argv))

    monkeypatch.chdir(tmp_path)
    sessions.cmd_resume(["codex", sid, "--cwd", str(forced_dir)])
    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(forced_dir))

    # listing now shows the pinned dir, not the session's original one
    recs = sessions.codex_light_records()
    row = sessions.resolve_row(recs[0])
    assert row[4] == str(forced_dir)

    # a later plain resume (back in some other dir, no --cwd) follows the pin
    monkeypatch.chdir(session_original_dir)
    sessions.cmd_resume(["codex", sid])

    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(forced_dir))
    assert exec_calls == [["codex", "resume", sid]] * 2
    assert "was pinned to" in capsys.readouterr().err


# ---------- list cache / resume by number ----------

def test_cmd_list_writes_numbered_cache(monkeypatch, tmp_path, capsys):
    kimi_home = tmp_path / ".kimi-code"
    kimi_home.mkdir()
    sess_dir = tmp_path / "sessdir"
    sess_dir.mkdir()
    (sess_dir / "state.json").write_text(json.dumps({"cwd": "/home/hunt", "updatedAt": 1700000000000}))
    (kimi_home / "session_index.jsonl").write_text(json.dumps({
        "sessionId": "session_abc123", "sessionDir": str(sess_dir),
    }) + "\n")
    monkeypatch.setattr(sessions, "KIMI_HOME", str(kimi_home))
    monkeypatch.setattr(sessions, "CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", str(tmp_path / "no-claude"))
    cache_file = tmp_path / "cache" / "last_list.json"
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(cache_file))

    sessions.cmd_list([])

    out = capsys.readouterr().out
    assert out.splitlines()[0].split()[0] == "#"
    assert out.splitlines()[1].split()[0] == "1"

    cached = json.loads(cache_file.read_text())
    assert cached == [{"tool": "kimi", "id": "session_abc123"}]


def test_resume_by_number_dispatches_correct_session(monkeypatch, tmp_path):
    cache_file = tmp_path / "last_list.json"
    cache_file.write_text(json.dumps([
        {"tool": "claude", "id": "aaaa"},
        {"tool": "kimi", "id": "session_bbbb"},
    ]))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(cache_file))

    calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: calls.append(argv))

    sessions.resume_by_number(2, ["-p", "hi"])

    assert calls == [["kimi", "-S", "session_bbbb", "-p", "hi"]]


def test_resume_by_number_hands_session_to_different_tool(monkeypatch, tmp_path):
    cache_file = tmp_path / "last_list.json"
    cache_file.write_text(json.dumps([{"tool": "kimi", "id": "session_bbbb"}]))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(cache_file))
    calls = []
    monkeypatch.setattr(
        sessions, "handoff_by_number",
        lambda n, target, extra: calls.append((n, target, extra)),
    )

    sessions.resume_by_number(1, ["codex", "-m", "gpt-5"])

    assert calls == [(1, "codex", ["-m", "gpt-5"])]


def test_resume_by_number_same_tool_resumes_without_redundant_arg(monkeypatch, tmp_path):
    cache_file = tmp_path / "last_list.json"
    cache_file.write_text(json.dumps([{"tool": "codex", "id": "cccc"}]))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(cache_file))
    calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: calls.append(argv))

    sessions.resume_by_number(1, ["codex"])

    assert calls == [["codex", "resume", "cccc"]]


def test_handoff_by_number_starts_target_in_source_cwd(monkeypatch, tmp_path, capsys):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    cache_file = tmp_path / "last_list.json"
    cache_file.write_text(json.dumps([{"tool": "kimi", "id": "session_bbbb"}]))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(
        sessions, "session_handoff_details", lambda _tool, _sid: (str(source_dir), "# Full conversation\nimportant end"),
    )
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(tmp_path / "handoffs"))
    calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: calls.append(argv))

    sessions.handoff_by_number(1, "codex", ["-m", "gpt-5"])

    assert os.path.realpath(os.getcwd()) == os.path.realpath(source_dir)
    assert calls[0][:3] == ["codex", "-m", "gpt-5"]
    export_path = tmp_path / "handoffs" / "kimi-session_bbbb.md"
    assert export_path.read_text() == "# Full conversation\nimportant end"
    assert str(export_path) in calls[0][-1]
    assert "complete conversation export" in calls[0][-1]
    assert "kimi row 1 -> codex" in capsys.readouterr().err


def test_handoff_by_number_to_kimi_uses_print_mode(monkeypatch, tmp_path, capsys):
    """Regression test: kimi has no bare positional prompt to seed an
    interactive session -- unlike claude/codex, passing one gets parsed as
    an attempted subcommand ("unknown command '<the whole prompt>'"). Its
    only way to accept a prompt at all is -p/--prompt."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    cache_file = tmp_path / "last_list.json"
    cache_file.write_text(json.dumps([{"tool": "codex", "id": "01a0"}]))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(
        sessions, "session_handoff_details", lambda _tool, _sid: (str(source_dir), "# Full conversation\nimportant end"),
    )
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(tmp_path / "handoffs"))
    calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: calls.append(argv))

    sessions.handoff_by_number(1, "kimi", [])

    assert calls[0][0] == "kimi"
    assert calls[0][1] == "-p"
    assert "complete conversation export" in calls[0][2]
    assert "no interactive prompt-seed option" in capsys.readouterr().err


def test_claude_handoff_exports_every_text_message(monkeypatch, tmp_path):
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text("\n".join(
        json.dumps({
            "type": "user" if i % 2 == 0 else "assistant",
            "message": {"content": [{"type": "text", "text": f"message {i}"}]},
            "cwd": str(tmp_path),
        })
        for i in range(75)
    ) + "\n")
    monkeypatch.setattr(sessions, "claude_light_records", lambda: [{
        "tool": "claude", "id": "claude-full", "path": str(transcript), "cwd": str(tmp_path),
    }])

    cwd, exported = sessions.session_handoff_details("claude", "claude-full")

    assert cwd == str(tmp_path)
    assert exported.count("## User") == 38
    assert exported.count("## Assistant") == 37
    assert "message 0" in exported
    assert "message 74" in exported


def test_resume_by_number_out_of_range(tmp_path, monkeypatch, capsys):
    cache_file = tmp_path / "last_list.json"
    cache_file.write_text(json.dumps([{"tool": "claude", "id": "aaaa"}]))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(cache_file))

    with pytest.raises(SystemExit):
        sessions.resume_by_number(5, [])
    assert "out of range" in capsys.readouterr().err


def test_resume_by_number_no_cache(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "does-not-exist.json"))

    with pytest.raises(SystemExit):
        sessions.resume_by_number(1, [])
    assert "no session list cached" in capsys.readouterr().err


def test_cmd_resume_routes_numeric_arg_to_resume_by_number(monkeypatch):
    calls = []
    monkeypatch.setattr(sessions, "resume_by_number", lambda n, extra, forced_cwd=None: calls.append((n, extra, forced_cwd)))

    sessions.cmd_resume(["3", "-p", "hi"])

    assert calls == [(3, ["-p", "hi"], None)]


# ---------- cmd_list argument validation ----------

def test_cmd_list_rejects_non_numeric_limit(capsys):
    with pytest.raises(SystemExit):
        sessions.cmd_list(["--limit", "not-a-number"])
    assert "expects a number" in capsys.readouterr().err


def test_cmd_list_rejects_unknown_tool(capsys):
    with pytest.raises(SystemExit):
        sessions.cmd_list(["--tool", "bogus"])
    assert "must be one of" in capsys.readouterr().err


def test_cmd_list_rejects_dangling_flag(capsys):
    with pytest.raises(SystemExit):
        sessions.cmd_list(["--limit"])
    assert "requires a value" in capsys.readouterr().err


def test_cmd_list_limit_all_shows_everything(monkeypatch, tmp_path, capsys):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    for i in range(30):
        write_codex_rollout(codex_home, f"0000000{i}-0000-0000-0000-00000000000{i}", cwd="/x", mtime=1000 + i)
    monkeypatch.setattr(sessions, "CODEX_HOME", str(codex_home))
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", str(tmp_path / "no-claude"))
    monkeypatch.setattr(sessions, "KIMI_HOME", str(tmp_path / "no-kimi"))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "cache" / "last_list.json"))

    sessions.cmd_list(["--limit", "all"])

    # header + 30 rows, comfortably more than the usual 20-row default
    assert len(capsys.readouterr().out.splitlines()) == 31


# ---------- cmd_stats ----------

def test_cmd_stats_shows_counts_and_directories(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sessions.time, "time", lambda: 1_800_000_000)
    monkeypatch.setattr(sessions, "claude_light_records", lambda: [
        {"tool": "claude", "id": "c1", "ts": 1_799_900_000, "cwd": "/home/hunt/work/aimux"},
        {"tool": "claude", "id": "c2", "ts": 1_799_950_000, "cwd": "/home/hunt"},
    ])
    monkeypatch.setattr(sessions, "codex_light_records", lambda: [
        {"tool": "codex", "id": "x1", "ts": 1_799_990_000, "cwd": "/home/hunt/work/aimux"},
    ])
    monkeypatch.setattr(sessions, "kimi_light_records", lambda _all: [])

    sessions.cmd_stats([])
    out = capsys.readouterr().out
    assert "Total sessions: 3" in out
    assert "claude  2" in out
    assert "codex   1" in out
    assert "Oldest:  1d ago" in out
    assert "Newest:  2h ago" in out
    assert "(2) /home/hunt/work/aimux" in out
    assert "(1) /home/hunt" in out


def test_cmd_stats_tool_filter(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sessions.time, "time", lambda: 1_800_000_000)
    monkeypatch.setattr(sessions, "claude_light_records", lambda: [
        {"tool": "claude", "id": "c1", "ts": 1_799_900_000, "cwd": "/home/hunt/work/aimux"},
    ])
    monkeypatch.setattr(sessions, "codex_light_records", lambda: [
        {"tool": "codex", "id": "x1", "ts": 1_799_990_000, "cwd": "/home/hunt/work/aimux"},
    ])
    monkeypatch.setattr(sessions, "kimi_light_records", lambda _all: [])

    sessions.cmd_stats(["--tool", "codex"])
    out = capsys.readouterr().out
    assert "Total sessions: 1" in out
    assert "codex   1" in out
    assert "claude" not in out.split("By tool:")[-1]


def test_cmd_stats_no_sessions(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sessions, "claude_light_records", lambda: [])
    monkeypatch.setattr(sessions, "codex_light_records", lambda: [])
    monkeypatch.setattr(sessions, "kimi_light_records", lambda _all: [])

    sessions.cmd_stats([])
    assert capsys.readouterr().out == "No sessions found.\n"


def test_cmd_stats_unknown_option_exits(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sessions, "claude_light_records", lambda: [])
    monkeypatch.setattr(sessions, "codex_light_records", lambda: [])
    monkeypatch.setattr(sessions, "kimi_light_records", lambda _all: [])

    with pytest.raises(SystemExit) as exc_info:
        sessions.cmd_stats(["--nope"])
    assert exc_info.value.code == 1
    assert "unknown option" in capsys.readouterr().err


# ---------- exec_or_die ----------

def test_exec_or_die_windows_runs_child_and_propagates_status(monkeypatch):
    calls = []
    monkeypatch.setattr(sessions.os, "name", "nt")
    monkeypatch.setattr(sessions.subprocess, "call", lambda argv: calls.append(argv) or 7)

    with pytest.raises(SystemExit) as exc_info:
        sessions.exec_or_die(["kimi", "-S", "session_123"])

    assert calls == [["kimi", "-S", "session_123"]]
    assert exc_info.value.code == 7


def test_exec_or_die_windows_handles_ctrl_c_without_traceback(monkeypatch):
    monkeypatch.setattr(sessions.os, "name", "nt")

    def interrupted(_argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(sessions.subprocess, "call", interrupted)

    with pytest.raises(SystemExit) as exc_info:
        sessions.exec_or_die(["kimi", "-S", "session_123"])

    assert exc_info.value.code == 130


def test_exec_or_die_missing_binary_reports_cleanly(monkeypatch, capsys):
    def fake_execvp(*_a, **_kw):
        raise FileNotFoundError()
    monkeypatch.setattr(sessions.os, "execvp", fake_execvp)

    with pytest.raises(SystemExit) as exc_info:
        sessions.exec_or_die(["not-a-real-binary", "--flag"])
    assert exc_info.value.code == 127
    assert "not found on PATH" in capsys.readouterr().err
