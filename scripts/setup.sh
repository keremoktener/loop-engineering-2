#!/usr/bin/env bash
# Preflight: verify the two external tools the loop depends on.
set -euo pipefail
ok=1
command -v codex >/dev/null 2>&1 && echo "✓ codex CLI found" || { echo "✗ codex CLI missing — install OpenAI Codex CLI"; ok=0; }
command -v gh   >/dev/null 2>&1 && echo "✓ gh CLI found"    || { echo "✗ gh CLI missing — https://cli.github.com"; ok=0; }
if command -v gh >/dev/null 2>&1; then
  gh auth status >/dev/null 2>&1 && echo "✓ gh authenticated" || { echo "✗ gh not authed — run: gh auth login"; ok=0; }
fi
python3 -c "import yaml" 2>/dev/null && echo "✓ pyyaml installed" || { echo "✗ pyyaml missing — pip install -r requirements.txt"; ok=0; }
[ "$ok" = 1 ] && echo "All set." || { echo "Fix the above, then re-run."; exit 1; }
