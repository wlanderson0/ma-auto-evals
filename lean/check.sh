#!/usr/bin/env bash
# The proof gate (plan step 4). Fails on any of:
#  - a change to the specification (Scope/Spec/SHA256SUMS) or to the gate itself
#    (GATE.SHA256SUMS: this script, Axioms.lean, the build files, the root import list);
#    either needs the same human review as the statements, then a new checksum;
#  - a proof file importing anything but Scope.* modules, or using a construct that can
#    change elaboration, add axioms or run code (list below);
#  - a statement or witness without a proof of exactly its type (Axioms.lean);
#  - a theorem depending on an axiom outside {propext, Classical.choice, Quot.sound}
#    (native computation shows up as an extra `_native` axiom; `sorry` as `sorryAx`);
#  - a warning, or leanchecker failing to replay the environment.
# Usage: LEAN=/path/to/lean ./check.sh   (leanchecker is taken from the same bin/)
set -euo pipefail
cd "$(dirname "$0")"
LEAN=${LEAN:-lean}
BIN=$(dirname "$(command -v "$LEAN")")
export PATH="$BIN:$PATH"

sha256sum --quiet -c Scope/Spec/SHA256SUMS || {
  echo "specification changed: re-review it, then update Scope/Spec/SHA256SUMS"; exit 1; }
sha256sum --quiet -c GATE.SHA256SUMS || {
  echo "the gate changed: re-review it, then update GATE.SHA256SUMS"; exit 1; }

# Every Lean file that is not specification: the proofs, the root, Axioms.lean.
mapfile -t checked < <(find Scope -name '*.lean' -not -path 'Scope/Spec/*' | sort; echo Scope.lean; echo Axioms.lean)
if grep -nE '^\s*import' "${checked[@]}" | grep -vE ':\s*import +Scope(\.[A-Za-z]+)*\s*$'; then
  echo "a proof file imports something other than Scope.*"; exit 1
fi
forbidden='set_option|«|\b(notation|infix|infixl|infixr|prefix|postfix|instance|macro|macro_rules|syntax|elab|elab_rules|implemented_by|extern|partial|unsafe|opaque|sorry|admit|native_decide|axiom|attribute|initialize|builtin_initialize|run_cmd|run_elab|run_meta|open private)\b|_elab\b|#eval|#exit|@\[ *(tactic|command_elab|term_elab|csimp|simp_proc)'
if grep -nE "$forbidden" "${checked[@]}"; then
  echo "forbidden construct in a proof file"; exit 1
fi

if command -v lake >/dev/null; then lake build --wfail; else LEAN="$LEAN" ./build.sh; fi

out=$(LEAN_PATH=.lake/build/lib/lean "$LEAN" Axioms.lean 2>&1) || {
  echo "$out"; echo "Axioms.lean failed to elaborate"; exit 1; }
echo "$out"
bad=$(echo "$out" | grep "depends on axioms" | sed 's/.*\[\(.*\)\]/\1/' | tr ',' '\n' \
  | sed 's/ //g' | grep -vxE 'propext|Classical.choice|Quot.sound' || true)
if [ -n "$bad" ]; then echo "axioms outside the allow-list: $bad"; exit 1; fi
expected=$(grep -c '^#print axioms' Axioms.lean)
reported=$(echo "$out" | grep -cE "depends on axioms|does not depend on any axioms")
if [ "$reported" -ne "$expected" ] || [ "$(echo "$out" | grep -vcE "axioms")" -ne 0 ]; then
  echo "expected exactly $expected axiom reports and nothing else"; exit 1
fi

LEAN_PATH=.lake/build/lib/lean leanchecker Scope
echo "proof gate passed"
