#!/usr/bin/env bash
# Build the Lean model without Lake: compiles each module in dependency order.
# CI uses `lake build` (lakefile.toml); this script is for machines with only the
# `lean` binary. Usage: LEAN=/path/to/lean ./build.sh [Module.Name ...]
set -euo pipefail
cd "$(dirname "$0")"
LEAN=${LEAN:-lean}
OUT=.lake/build/lib/lean
mkdir -p "$OUT"
export LEAN_PATH="$OUT${LEAN_PATH:+:$LEAN_PATH}"
modules=("$@")
if [ ${#modules[@]} -eq 0 ]; then
  mapfile -t modules < <(grep -E '^import Scope\.' Scope.lean | sed 's/^import //')
  modules+=("Scope")
fi
for module in "${modules[@]}"; do
  src="${module//.//}.lean"
  olean="$OUT/${module//.//}.olean"
  mkdir -p "$(dirname "$olean")"
  "$LEAN" -R . -o "$olean" "$src"
done
