#!/usr/bin/env bash
# ledger.sh <wave> <feature> <role> <model> <tokens> [tool_uses] [note]
# ledger.sh --summary <wave>
#
# Append one usage row per agent call to .claude/runs/ledger.tsv (creating it
# with a header row if missing), or print per-feature and per-role token and
# tool-use totals for a wave.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
LEDGER="$REPO/.claude/runs/ledger.tsv"
HEADER="timestamp	wave	feature	role	model	tokens	tool_uses	note"

if [ "${1:-}" = "--summary" ]; then
  WAVE="${2:?usage: ledger.sh --summary <wave>}"
  [ -f "$LEDGER" ] || { echo "ledger: no ledger at $LEDGER" >&2; exit 1; }
  WAVE="$WAVE" python3 - "$LEDGER" <<'PY'
import csv, os, sys
from collections import defaultdict
import statistics

path = sys.argv[1]
wave = os.environ["WAVE"]
by_feature = defaultdict(lambda: [0, 0])
by_role = defaultdict(lambda: [0, 0])
rows_by_role = defaultdict(list)
all_rows = []

with open(path, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f, delimiter="\t"):
        if row.get("wave") != wave:
            continue
        tokens = int(row.get("tokens") or 0)
        tools = int(row.get("tool_uses") or 0)
        by_feature[row["feature"]][0] += tokens
        by_feature[row["feature"]][1] += tools
        by_role[row["role"]][0] += tokens
        by_role[row["role"]][1] += tools
        rows_by_role[row["role"]].append((tokens, row))
        all_rows.append((tokens, row))

if not by_feature:
    print(f"ledger: no rows for wave {wave}")
    sys.exit(0)

print(f"wave {wave}")
print("by feature:")
for feature, (tokens, tools) in sorted(by_feature.items()):
    print(f"  {feature}: {tokens} tokens, {tools} tool uses")
print("by role:")
for role, (tokens, tools) in sorted(by_role.items()):
    print(f"  {role}: {tokens} tokens, {tools} tool uses")
total_tokens = sum(v[0] for v in by_feature.values())
total_tools = sum(v[1] for v in by_feature.values())
print(f"total: {total_tokens} tokens, {total_tools} tool uses")

# Detect outliers: rows with tokens > 2x the median for their role
role_medians = {}
for role, token_rows in rows_by_role.items():
    if len(token_rows) >= 2:
        tokens_list = [t for t, _ in token_rows]
        role_medians[role] = statistics.median(tokens_list)

outlier_rows = []
for tokens, row in all_rows:
    role = row["role"]
    if role in role_medians and tokens > 2 * role_medians[role]:
        outlier_rows.append((tokens, row))

print("outliers:")
if outlier_rows:
    for tokens, row in sorted(outlier_rows, key=lambda x: -x[0]):
        print(f"  {row['feature']} {row['role']}: {tokens} tokens")
else:
    print("  none")
PY
  exit 0
fi

WAVE="${1:?usage: ledger.sh <wave> <feature> <role> <model> <tokens> [tool_uses] [note]}"
FEATURE="${2:?usage: ledger.sh <wave> <feature> <role> <model> <tokens> [tool_uses] [note]}"
ROLE="${3:?usage: ledger.sh <wave> <feature> <role> <model> <tokens> [tool_uses] [note]}"
MODEL="${4:?usage: ledger.sh <wave> <feature> <role> <model> <tokens> [tool_uses] [note]}"
TOKENS="${5:?usage: ledger.sh <wave> <feature> <role> <model> <tokens> [tool_uses] [note]}"
TOOL_USES="${6:-}"
NOTE="${7:-}"
NOTE="$(printf '%s' "$NOTE" | tr '\t\n' '  ')"

mkdir -p "$(dirname "$LEDGER")"
if [ ! -f "$LEDGER" ]; then
  printf '%s\n' "$HEADER" > "$LEDGER"
fi

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$TS" "$WAVE" "$FEATURE" "$ROLE" "$MODEL" "$TOKENS" "$TOOL_USES" "$NOTE" >> "$LEDGER"
echo "ledger: recorded $WAVE/$FEATURE/$ROLE"
