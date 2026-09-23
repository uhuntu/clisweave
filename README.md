# clisweave

[![test](https://github.com/uhuntu/clisweave/actions/workflows/test.yml/badge.svg)](https://github.com/uhuntu/clisweave/actions/workflows/test.yml)

A tiny, dependency-free wrapper that weaves three AI coding-agent CLIs — [Claude Code](https://claude.com/product/claude-code), [OpenAI Codex CLI](https://github.com/openai/codex), and [Kimi CLI](https://www.kimi-cli.com/) — behind one set of flags, plus cross-tool session discovery, resume, and handoff.

No daemon, no config file, no build step — just a small Python package (`src/clisweave/`) that reads each tool's own on-disk session store directly.

![clisweave demo: a unified session list across claude/codex/kimi, then an LLM-judged topic search narrowing it down to the one relevant session](assets/demo.gif)

## Install

**Via pip** (the package is named `clisweave` on PyPI; the commands installed are `ai`, `cw`, `clisweave`, `ai-sessions`, plus the legacy `aim` and `aimux` aliases):

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

Whichever of the last three you use, it clones the repo to `~/.local/share/clisweave` first (override with `CLISWEAVE_REPO_DIR`), then wires up `ai`, `cw`, `clisweave`, `ai-sessions`, and the legacy `aim`/`aimux` aliases in `~/.local/bin` (override with `CLISWEAVE_BIN_DIR`) — as symlinks on `install.sh`, or native `.cmd` launchers on `install.ps1`. To avoid taking over an unrelated command, the standalone installers skip `cw` with a warning if it already exists. The old `AIMUX_REPO_DIR` and `AIMUX_BIN_DIR` variables remain accepted for compatibility. Nothing is copied — the clone stays the source of truth.

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
ai sessions --all           # include archived sessions and ones a tool started for itself

ai resume kimi 97946bc7     # resume by short id / prefix (resolved against real session ids)
ai resume claude            # no id -> tool's own interactive picker
ai resume 3                 # resume row 3 from the last `ai`/`ai sessions` listing
ai 3 codex                  # hand row 3's context to a new Codex session

ai search "the nfc frequency lock issue"   # find sessions relevant to a topic
ai search "katago" --tool claude           # restrict the candidates to one tool
ai search "..." --judge kimi               # use a different model to judge relevance
ai search "..." --why                      # print each hit's reason in full, on its own line
ai search "..." --all                      # list every hit, not just the strongest 10

ai stats                    # session counts per tool, oldest/newest, top directories
ai stats --tool claude      # stats for one tool only
```

Sessions a tool started for itself are left out of `ai sessions` and `ai search`: Codex's command-approval reviews (one per command it asks you to approve) and `ai search`'s own judge runs. Neither is a conversation of yours, and a judge session literally contains every candidate's text, which makes it match nearly any topic. They stay reachable by id (`ai resume codex <id>`), and `ai sessions --all` lists them anyway.

Every `ai`/`ai sessions` listing is numbered and cached, so `ai resume <N>` is usually the fastest way in: run `ai`, glance at the row you want, `ai resume 3`. The cache is just the last listing you saw — it's overwritten by the next `ai sessions` call and doesn't try to detect if the underlying sessions changed since.

To switch agents, put the target tool after the row number: `ai 3 codex`. Clisweave exports the complete textual conversation to `~/.cache/clisweave/handoffs/`, changes to its original working directory, and starts a new target-tool session with a prompt that asks it to read the export, summarize it, and continue the work. If the named tool already owns that row, the command simply resumes the original session. In listings, a session started this way is titled `(handoff) <topic>` after the session it continues (following a chain of handoffs back to the original), rather than by its own generated "Continue codex session ..." seed.

One exception: kimi cannot open an interactive session with an initial prompt (a bare prompt parses as a subcommand name), so a handoff to it runs the seed as a one-shot `kimi -p` and then automatically resumes the session that run persisted (`kimi -S <id>`), dropping you into the interactive continuation with the summary already in its history. If the seed run fails, nothing is resumed — the error is reported, and you pick up with `kimi -c`.

### How `ai search` works

`ai search` runs two complementary passes:

1. **Exact pre-pass** — a case-insensitive scan of conversation text, tool calls, results, and kimi background-task output logs. It ignores session metadata and injected instructions. Short alphabetic queries such as `cra` match whole words, so they do not match `craft` or `crash`; longer queries retain substring matching. Results print under `exact matches` with zero LLM cost.

2. **Semantic pass** — the LLM judge. Titles alone miss a lot — plenty of sessions are titled "hi" or "(no title)", and the relevant sessions may never use your exact words. So each candidate's tool, cwd, title, and a short content snippet go into one prompt, and an LLM (`claude -p` by default) picks out which numbers are relevant. One batched call, not one call per session — with 100+ sessions, calling an LLM separately for each would be far too slow and far too expensive. That also means it costs one real LLM call (tokens, however your `claude`/`codex`/`kimi` account bills them) every time you run it. Results print under `semantic matches`.

The exact pass exists because the semantic pass reasons over small *sampled* snippets, and a term that only appears in unsampled messages, tool calls, or past the snippet scan cap is invisible to the judge — a real `aria2c` search missed 4 sessions that grepping found immediately. The two passes union (a session listed as exact is not repeated under semantic), and both count for `ai resume <N>`.

Search skips Codex approval-review sessions whose prompts quote another agent's history. Those copies otherwise appear as duplicate matches. Row numbers continue across the exact and semantic sections, so each displayed number matches `ai resume <N>`.

The judge reasons about more than just keyword overlap — e.g. searching "katago" correctly pulled in sessions with generic titles like "hi" or "(no title)" that were run inside the `katago` project directory, which plain text search would have missed entirely.

It is also asked to name its strongest hit first, and that order is what gets printed — recency only breaks ties. Ranking by how recently a session ran put the one session actually about the topic below older ones that merely mentioned it. Only the strongest 10 are listed (`--all` shows the rest), since the tail is the weakest of them and a long list is what made a broad search hard to scan.

It must also point at something concrete — a file, command, error, or version — rather than count a session whose only link is the directory it ran in. Asked for that, the same search still returns `Hello` and `Yes go` rows from `android-vts`, but now with the specific commit behind each (`A13 VTS NFC HAL OpenAfterOpen fix`), which is what makes them worth keeping.

It has to say *why* each match counts: the judge answers one line per match (`7: upgrades the firmware from A13`) rather than a bare list of numbers, which makes it commit to a link instead of ticking a box. The reason prints as a WHY column beside the hit, clipped to whatever width the terminal has left over, so ten hits stay ten lines. `--why` prints it in full on its own line instead. That column is what makes a hit with a useless title readable: a row titled `Hello` in an `android-vts` directory is a real A13 VTS fix (`A13 VTS NFC HAL OpenAfterOpen fix committed to the A13 SDK`), which nothing else in the row suggests.

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

`kimi -S <id>` refuses to resume a session from a different directory than the one it was created in. `ai resume`/`ai <N>` know each session's original directory already (it's the CWD column), so for all three tools they `cd` there automatically before resuming, rather than leaving you to do it by hand (or, for kimi, surfacing its hard error).

Pass `--cwd <dir>` to send it somewhere else instead, e.g. `ai resume 2 --cwd /path/to/other-project`. claude, codex, and kimi all tie a session's transcript permanently to whichever directory it first ran in — confirmed by testing `claude --resume` from an unrelated directory: the resumed turn was appended to the *original* directory's log, nothing was written under the new one — so a session can't actually be relocated in place. If `--cwd` points at a directory other than the one the session already lives in, `ai resume` recognizes that a plain `--resume` there wouldn't accomplish anything (it'd work, but the conversation would still be invisible to that directory's own `/resume` picker) and instead does a handoff: it exports the full transcript and starts a **new**, freshly-seeded session in `<dir>` — same as `ai <N> <other-tool>`, but staying on the same tool. That new session is a real one rooted in `<dir>`, so it shows up in `/resume` there going forward.

## Development

```bash
pip install -e ".[test]"
pytest tests/ -v
```

## What this isn't

Not a TUI, not a worktree manager, not a multi-agent orchestrator. If you want any of that, look at [ccmanager](https://github.com/kbwo/ccmanager) (git-worktree-centric session manager, also supports Kimi CLI) or [claude-squad](https://github.com/smtg-ai/claude-squad).

## License

MIT — see [LICENSE](LICENSE).
