#!/usr/bin/env bash
# Usage: tests/smoke.sh <lrdmem-cmd>   e.g. tests/smoke.sh "python -m lrdmem"
# Runs --version and lint against a throwaway fixture store; non-zero on failure.
set -euo pipefail
cmd=${1:?usage: smoke.sh <lrdmem-cmd>}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/store/_global"
printf -- '- [g](g.md) — smoke fact\n' > "$tmp/store/_global/MEMORY.md"
cat > "$tmp/store/_global/g.md" <<'MEM'
---
name: g
description: smoke fixture
metadata:
  type: user
  author: smoke
  updated: 2026-01-01
---
**Why:** exercise lint.
**How to apply:** nothing.
MEM
export LRDMEM_STORE="$tmp/store" LRDMEM_CONFIG="$tmp/config.json"
$cmd --version
$cmd lint
echo "smoke ok"
