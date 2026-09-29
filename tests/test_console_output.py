"""Output that outlives the console it was printed to.

A session title is whatever was typed into that session, and the console's
code page decides how much of it can be printed: on a GBK console a title
beginning with a bullet (•) raised UnicodeEncodeError six rows into `ai`'s
own listing -- the one command with no other way to look at your sessions.
The read side of the same problem (a transcript byte that is not valid
UTF-8) is already handled by open_text; these cover the write side.
"""

import io
import sys

import pytest

from clisweave import cli, sessions


def _gbk_stdout():
    """A console like a Chinese Windows one: GBK, strict errors."""
    return io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")


def test_a_title_the_console_cannot_encode_still_prints(monkeypatch):
    """The real incident: `ai` on a GBK console with a session titled
    "• Two things to report ..." among the rows."""
    stream = _gbk_stdout()
    monkeypatch.setattr(sys, "stdout", stream)

    sessions.harden_console_output()
    sessions.render_rows(
        [("step", "id-1", "1h ago", "id-1", "C:\\work", "• Two things to report")],
        write_cache=False,
    )
    stream.flush()

    # the bullet is gone; the row around it is not
    assert b"? Two things to report" in stream.buffer.getvalue()


def test_characters_the_console_can_encode_are_left_alone(monkeypatch):
    """Replacing with "?" must not become "encode nothing": the console's
    own code page still shows what it can (GBK has the em-dash)."""
    stream = _gbk_stdout()
    monkeypatch.setattr(sys, "stdout", stream)

    sessions.harden_console_output()
    print("commit — the A13 fix")
    stream.flush()

    assert "commit — the A13 fix".encode("gbk") in stream.buffer.getvalue()


def test_both_entry_points_harden_the_output_before_printing(monkeypatch, capsys):
    """`ai` and `ai-sessions` are two separate console scripts; hardening
    one of them leaves the other aborting on the same title."""
    calls = []
    monkeypatch.setattr(sessions, "harden_console_output", lambda: calls.append(1))

    monkeypatch.setattr(sys, "argv", ["ai", "--version"])
    cli.main()

    monkeypatch.setattr(sys, "argv", ["ai-sessions"])
    with pytest.raises(SystemExit):
        sessions.main()

    assert len(calls) == 2


def test_hardening_survives_a_stream_it_cannot_reconfigure(monkeypatch):
    """sys.stdout is not always a text stream -- pytest's capture, pythonw,
    an embedded host. The guard must not become the crash it prevents."""
    class NotAStream:
        pass

    monkeypatch.setattr(sys, "stdout", NotAStream())
    monkeypatch.setattr(sys, "stderr", NotAStream())

    sessions.harden_console_output()  # must not raise
