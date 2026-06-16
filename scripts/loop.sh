#!/usr/bin/env bash
# Convenience driver: spec → plan → (human gate) → loop → verify
set -euo pipefail
INTENT="${1:?usage: scripts/loop.sh \"<one-line intent>\"}"
python3 -m loopengine spec "$INTENT"
echo; read -rp "Edit .loop/spec.md if needed, then press Enter to plan…" _
python3 -m loopengine plan
echo; read -rp "Review the plan + issues. Enter to start the autonomous loop…" _
python3 -m loopengine loop
python3 -m loopengine verify
