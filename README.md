# clisweave

[![test](https://github.com/uhuntu/clisweave/actions/workflows/test.yml/badge.svg)](https://github.com/uhuntu/clisweave/actions/workflows/test.yml)

A tiny, dependency-free wrapper that weaves three AI coding-agent CLIs — [Claude Code](https://claude.com/product/claude-code), [OpenAI Codex CLI](https://github.com/openai/codex), and [Kimi CLI](https://www.kimi-cli.com/) — behind one set of flags, plus cross-tool session discovery, resume, and handoff.

No daemon, no config file, no build step — just a small Python package (`src/clisweave/`) that reads each tool's own on-disk session store directly.

![clisweave demo: a unified session list across claude/codex/kimi, then an LLM-judged topic search narrowing it down to the one relevant session](assets/demo.gif)

## Install

**Via pip** (the package is named `clisweave` on PyPI; the commands installed are `ai`, `clisweave`, `ai-sessions`, plus the legacy `aim` and `aimux` aliases):

```bash
pip install clisweave
```

**Via curl** (macOS/Linux, or Windows with Git Bash/WSL), one line, no manual clone:

```bash
curl -fsSL https://raw.githubusercontent.com/uhuntu/clisweave/master/install.sh | bash
```

**Via irm** (Windows PowerShell, no Git Bash/WSL/Cygwin needed):

```powershell
irm https://raw.githubusercontent.com/uhuntu/clisweave/master/install.ps1 | iex
```

**Via git**, if you'd rather clone it yourself first:

```bash
git clone https://github.com/uhuntu/clisweave.git
cd clisweave && ./install.sh        # Windows PowerShell: .\install.ps1
```

Whichever of the last three you use, it clones the repo to `~/.local/share/clisweave` first (override with `CLISWEAVE_REPO_DIR`), then wires up `ai`, `clisweave`, `ai-sessions`, and the legacy `aim`/`aimux` aliases in `~/.local/bin` (override with `CLISWEAVE_BIN_DIR`) — as symlinks on `install.sh`, or native `.cmd` launchers on `install.ps1`. The old `AIMUX_REPO_DIR` and `AIMUX_BIN_DIR` variables remain accepted for compatibility. Nothing is copied — the clone stays the source of truth.

Requires `claude`, `codex`, and/or `kimi` already installed and on `PATH` (only the ones you actually use need to be present).

> **Windows note:** running the curl one-liner from PowerShell/cmd (rather than Git Bash) can invoke the WSL `bash` launcher by mistake instead of Git's — use `irm` above, or run curl from Git Bash directly. `install.sh` also copes if Git Bash lacks symlink privilege (falls back to a generated launcher instead of a broken copy) or `python3` on `PATH` is the Microsoft Store's no-op stub (probes `python`/`py -3` instead). Files installed by `install.sh` are still extensionless with a shebang line, though, which PowerShell can't execute directly — `install.ps1`'s `.cmd` launchers don't have that problem. If you stick with `install.sh`, call `ai` from Git Bash instead, or add a function to your PowerShell `$PROFILE`:
> ```powershell
> function ai { & "C:\Path\To\python.exe" "$HOME\.local\share\clisweave\bin\ai" @args }
> ```


## Update

```bash
ai update        # update clisweave itself
ai update tools  # update claude, codex, and kimi (whichever are installed)
ai update all    # both
```

`ai update` detects how clisweave itself was installed and does the right thing: `git pull --ff-only` for a curl/git install, `pip install --upgrade clisweave` for a pip install.

`ai update tools` runs each CLI's own update command (`claude update`, `codex update`, `kimi update`), skipping any that aren't installed. If one fails, the others still run; the exit code reflects the worst failure.

Equivalent manual commands for updating clisweave itself, if you'd rather:

- **pip**: `pip install --upgrade clisweave`
- **curl**: re-run the same one-liner — it fast-forwards the existing clone before relinking
- **git**: `git -C /path/to/clisweave pull` — the symlinks point straight into the repo, so this alone is enough

## Usage

```bash
ai                          # recent sessions across all three tools (same as `ai sessions`)
ai claude -p "prompt"       # -> claude -p "prompt"
ai codex -p -m o3 "prompt"  # -> codex exec -m o3 "prompt"
ai kimi -c                  # -> kimi -c

ai sessions --limit 10      # list recent sessions, all tools
ai sessions --limit all     # no cutoff -- same as `ai full`
ai full                     # shorthand for `ai sessions --limit all`
ai sessions --tool codex    # filter to one tool
ai sessions --cwd           # only sessions started in the current directory
ai sessions --all           # include archived sessions

ai resume kimi 97946bc7     # resume by short id / prefix (resolved against real session ids)
ai resume claude            # no id -> tool's own interactive picker
ai resume 3                 # resume row 3 from the last `ai`/`ai sessions` listing
ai 3 codex                  # hand row 3's context to a new Codex session

ai search "the nfc frequency lock issue"   # find sessions relevant to a topic
ai search "katago" --tool claude           # restrict the candidates to one tool
ai search "..." --judge kimi               # use a different model to judge relevance

ai stats                    # session counts per tool, oldest/newest, top directories
ai stats --tool claude      # stats for one tool only
```

Every `ai`/`ai sessions` listing is numbered and cached, so `ai resume <N>` is usually the fastest way in: run `ai`, glance at the row you want, `ai resume 3`. The cache is just the last listing you saw — it's overwritten by the next `ai sessions` call and doesn't try to detect if the underlying sessions changed since.

To switch agents, put the target tool after the row number: `ai 3 codex`. Clisweave exports the complete textual conversation to `~/.cache/clisweave/handoffs/`, changes to its original working directory, and starts a new target-tool session with a prompt that asks it to read the export, summarize it, and continue the work. If the named tool already owns that row, the command simply resumes the original session.

### How `ai search` works

Titles alone miss a lot — plenty of sessions are titled "hi" or "(no title)". So `ai search` doesn't grep for your exact words; it builds one prompt listing every candidate session's tool, cwd, title, and a short content snippet, and asks an LLM (`claude -p` by default) to pick out which numbers are actually relevant to your topic. One batched call, not one call per session — with 100+ sessions, calling an LLM separately for each would be far too slow and far too expensive. That also means it costs one real LLM call (tokens, however your `claude`/`codex`/`kimi` account bills them) every time you run it.

It reasons about more than just keyword overlap — e.g. searching "katago" correctly pulled in sessions with generic titles like "hi" or "(no title)" that were run inside the `katago` project directory, which plain text search would have missed entirely.

### Normalized flags (`ai <tool> ...`)

| Flag | Meaning | claude | codex | kimi |
|---|---|---|---|---|
| `-p`, `--print` | non-interactive, print and exit | `-p` | `exec` | `-p` |
| `-c`, `--continue` | continue most recent session in cwd | `--continue` | `exec resume --last` | `-c` |
| `-m`, `--model <model>` | model to use | `--model` | `-m` | `-m` |
| `--add-dir <dir>` | additional workspace directory (repeatable) | `--add-dir` | `--add-dir` | `--add-dir` |
| `-y`, `--yolo` | auto-approve tool calls | `--dangerously-skip-permissions` | `--approve-for-me` (stays sandboxed) | `-y` |

Anything after a literal `--`, or any flag this wrapper doesn't recognize, passes straight through to the underlying CLI unchanged.

## How session listing works

`ai-sessions` reads each tool's native session storage — no shared index, no background process:

- **claude**: `~/.claude/projects/*/*.jsonl`
- **codex**: `~/.codex/session_index.jsonl` + `~/.codex/sessions/**/*.jsonl` for cwd lookup
- **kimi**: `~/.kimi-code/session_index.jsonl` + each session's `state.json` / `agents/main/wire.jsonl`

Titles are best-effort (scanned from the first user message / prompt in each session's log). Claude's cwd is read from the session content itself when available, falling back to a guess decoded from the project-directory name only if that's missing.

`kimi -S <id>` refuses to resume a session from a different directory than the one it was created in. `ai resume`/`ai <N>` know each kimi session's original directory already (it's the CWD column), so they `cd` there automatically before resuming instead of surfacing that error.

## Development

```bash
pip install -e ".[test]"
pytest tests/ -v
```

## What this isn't

Not a TUI, not a worktree manager, not a multi-agent orchestrator. If you want any of that, look at [ccmanager](https://github.com/kbwo/ccmanager) (git-worktree-centric session manager, also supports Kimi CLI) or [claude-squad](https://github.com/smtg-ai/claude-squad).

## License

MIT — see [LICENSE](LICENSE).
