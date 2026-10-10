"""Terminal color, decided once and off unless the terminal asks for it.

The listing is the one screen you look at every time you type `ai`, and a
table where every column weighs the same gives the eye nowhere to go. So
the tool column gets a hue, the plumbing (id, when, turns) goes dim, and
the rows that belong to the directory you are standing in stand out.

None of that may leak escape codes into a pipe, a file, a CI log or a
console that cannot render them -- which is most of them. Color is on only
when the output stream says it is a terminal, the environment has not said
no (NO_COLOR, TERM=dumb), and CLISWEAVE_COLOR has not overridden it. The
probe result is cached per process; tests set `_STATE` directly.

No dependency is added for this (the README promises dependency-free), so
the Windows side enables virtual-terminal processing on the console handle
itself with ctypes rather than pulling in colorama. If that fails the
console simply stays monochrome, which is exactly today's behavior.
"""
import os
import sys

# SGR parameters, not sequences: "\033[" + code + "m" is assembled by _sgr
# so that a disabled palette can return "" for every code at once.
BOLD = "1"
DIM = "2"
RESET = "0"

# One hue per tool. 256-color values when the terminal has them, basic
# colors otherwise -- close enough hues that the table still reads the same.
TOOL_256 = {"claude": "214", "codex": "114", "kimi": "117", "step": "141",
            "zcode": "110", "codebuddy": "75"}
TOOL_BASIC = {"claude": "33", "codex": "32", "kimi": "36", "step": "35",
              "zcode": "34", "codebuddy": "94"}

_STATE = None  # None = not probed yet, True/False once decided


def _has_256():
    term = os.environ.get("TERM", "")
    if "256color" in term:
        return True
    return os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")


def _enable_windows_vt():
    """Let a Windows console interpret SGR sequences at all.

    cmd.exe and PowerShell only process ANSI escapes when the console mode
    has ENABLE_VIRTUAL_TERMINAL_PROCESSING set, and Python does not set it.
    Without this the "color" is literal "^[32m" text in the output, which is
    worse than no color -- so a failure here disables color instead."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        stdout_handle = -11  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(kernel32.GetStdHandle(stdout_handle),
                                       ctypes.byref(mode)):
            return False  # not a console (already checked isatty, but be sure)
        return bool(kernel32.SetConsoleMode(kernel32.GetStdHandle(stdout_handle),
                                            mode.value | 0x0004))
    except Exception:
        return False


def _decide(stream=None):
    """True when color should be emitted to `stream` (default stdout)."""
    override = os.environ.get("CLISWEAVE_COLOR", "auto").strip().lower()
    if override in ("never", "no", "off", "0"):
        return False
    stream = stream if stream is not None else sys.stdout
    forced = override in ("always", "force", "on", "1")
    try:
        tty = stream.isatty()
    except Exception:
        tty = False  # a stream that cannot say is not a terminal
    if not forced:
        if not tty:
            return False
        if os.environ.get("NO_COLOR") is not None:
            return False
        if os.environ.get("TERM", "").lower() == "dumb":
            return False
    # Only a console has to be told to interpret SGR sequences; a pipe or a
    # file just carries the bytes to whatever reads them next, which may well
    # be a terminal (`ai | less -R`). Failing to enable VT on a console
    # disables color rather than leaking literal escape text.
    return _enable_windows_vt() if tty else True


def enabled(stream=None):
    """Cached answer to _decide -- the probe touches the console handle."""
    global _STATE
    if _STATE is None:
        _STATE = _decide(stream)
    return _STATE


def _sgr(code):
    return f"\033[{code}m" if enabled() else ""


def paint(text, *codes):
    """text wrapped in the given SGR codes, or unchanged when color is off."""
    if not enabled() or not codes:
        return text
    return "".join(_sgr(c) for c in codes) + text + _sgr(RESET)


def tool_color(tool):
    """The SGR foreground code for a tool's hue (basic or 256)."""
    table = TOOL_256 if _has_256() else TOOL_BASIC
    return table.get(tool, "37")
