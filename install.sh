#!/usr/bin/env bash
# Symlinks Clisweave's commands (ai, ai-sessions, plus optional aliases)
# into a directory on your PATH.
#
# Run after cloning:
#   ./install.sh
#
# Or as a one-liner, which clones the repo first:
#   curl -fsSL https://raw.githubusercontent.com/uhuntu/clisweave/master/install.sh | bash
set -euo pipefail

# On Git-for-Windows/MSYS, `ln -sf` defaults to copying instead of creating a
# real symlink unless told otherwise -- this asks for a real one (still needs
# Developer Mode or admin; the check further down falls back to a launcher
# if even that isn't available). Meaningless outside MSYS, so harmless
# elsewhere.
export MSYS="${MSYS:-}${MSYS:+ }winsymlinks:nativestrict"

REPO_URL="https://github.com/uhuntu/clisweave.git"
BIN_DIR="${CLISWEAVE_BIN_DIR:-${AIMUX_BIN_DIR:-$HOME/.local/bin}}"

# When piped via `curl | bash`, there's no script file, so BASH_SOURCE[0] is
# unset (not just non-matching) -- guard the lookup instead of dereferencing
# it directly under `set -u`.
SOURCE_PATH="${BASH_SOURCE[0]:-}"
if [[ -n "$SOURCE_PATH" && -f "$(dirname "$SOURCE_PATH")/bin/ai" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "$SOURCE_PATH")" && pwd)"
else
  # Not running from inside a clone -- fetch one first, then re-run from there.
  REPO_DIR="${CLISWEAVE_REPO_DIR:-${AIMUX_REPO_DIR:-$HOME/.local/share/clisweave}}"
  if [[ -d "$REPO_DIR/.git" ]]; then
    git -C "$REPO_DIR" pull --ff-only
  else
    git clone --depth 1 "$REPO_URL" "$REPO_DIR"
  fi
  exec "$REPO_DIR/install.sh"
fi

mkdir -p "$BIN_DIR"

# On Windows, `ln -sf` needs symlink privilege (Developer Mode) or it silently
# falls back to *copying* the file -- which breaks ai/ai-sessions, since they
# locate their package by resolving their own realpath. And the `python3`
# name on PATH is sometimes a no-op Microsoft Store stub even when a real
# interpreter is installed as `python`. Detect both and fall back to a tiny
# launcher that hardcodes a working interpreter and the real script's path,
# instead of depending on symlink support or the `python3` name resolving.
PY_LAUNCHER=""
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c "" >/dev/null 2>&1; then
  for cand in python python3.12 python3.11 python3.10; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "" >/dev/null 2>&1; then
      PY_LAUNCHER="$(command -v "$cand")"
      break
    fi
  done
fi

# A path like /c/Users/... means something to MSYS's own tools, but native
# Windows Python sees a leading "/" as "root of the current drive" and
# mangles it -- so a path we're about to embed in a launcher script (as
# opposed to passing as argv, which MSYS translates automatically) needs
# converting to a real Windows path first. `pwd -W` is Git-for-Windows/MSYS
# only; elsewhere (real POSIX) the path is already fine as-is.
to_native_path() {
  local p="$1" dir base winpath
  dir="$(dirname "$p")"
  base="$(basename "$p")"
  if winpath="$(cd "$dir" 2>/dev/null && pwd -W 2>/dev/null)"; then
    printf '%s/%s' "$winpath" "$base"
  else
    printf '%s' "$p"
  fi
}

link_or_launcher() {
  local src="$1" dst="$2"
  ln -sf "$src" "$dst" || true
  if [[ -L "$dst" && -z "$PY_LAUNCHER" ]]; then
    echo "linked $dst -> $src"
    return
  fi
  # Fallback: either ln -sf didn't produce a real symlink (e.g. no Windows
  # symlink privilege) or the plain `python3` shebang isn't usable. Either
  # way, replace $dst with a launcher that runs $src fresh every time (so
  # the clone stays the source of truth, same as a symlink would).
  local py="${PY_LAUNCHER:-python3}"
  local native escaped
  native="$(to_native_path "$src")"
  escaped="$(printf '%s' "$native" | sed "s/\\\\/\\\\\\\\/g; s/'/\\\\'/g")"
  rm -f "$dst"
  printf '#!%s\nimport runpy\nrunpy.run_path(%s, run_name="__main__")\n' \
    "$py" "'$escaped'" > "$dst"
  chmod +x "$dst"
  echo "linked $dst -> $src (launcher)"
}

for name in ai ai-sessions; do
  link_or_launcher "$SCRIPT_DIR/bin/$name" "$BIN_DIR/$name"
done

if [[ -e "$BIN_DIR/cw" || -L "$BIN_DIR/cw" ]]; then
  echo "Warning: $BIN_DIR/cw already exists; leaving it unchanged" >&2
else
  link_or_launcher "$SCRIPT_DIR/bin/ai" "$BIN_DIR/cw"
fi

for alias_name in clisweave aim aimux; do
  link_or_launcher "$SCRIPT_DIR/bin/ai" "$BIN_DIR/$alias_name"
done

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Note: $BIN_DIR is not on your PATH. Add it in your shell rc file, e.g.:" ;
     echo "  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

echo "Done. Try: ai --help"
