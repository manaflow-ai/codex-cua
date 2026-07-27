#!/usr/bin/env bash
# Symlink codex-cua onto PATH.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$here/bin/codex-cua"
dest_dir="${CODEX_CUA_BIN_DIR:-$HOME/.local/bin}"
dest="$dest_dir/codex-cua"

[ -x "$src" ] || { echo "missing executable: $src" >&2; exit 1; }
mkdir -p "$dest_dir"
ln -sfn "$src" "$dest"
echo "linked $dest -> $src"

case ":$PATH:" in
  *":$dest_dir:"*) ;;
  *) echo "warning: $dest_dir is not on PATH" >&2 ;;
esac

"$dest" --version >/dev/null
echo "run: codex-cua doctor"
