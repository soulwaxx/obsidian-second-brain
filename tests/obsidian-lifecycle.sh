#!/usr/bin/env bash
set -euo pipefail

hook=${1:?hook path required}
middleware=${2:?middleware path required}
hook=$(cd "$(dirname "$hook")" && pwd -P)/$(basename "$hook")
middleware=$(cd "$middleware" && pwd -P)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
vault=$work/vault
mkdir -p "$vault/wiki" "$vault/.obsidian"
cd "$vault"
git init -q
git config user.name test
git config user.email test@example.invalid

cat >wiki/quickstart.md <<'EOF'
---
type: note
title: Quickstart
last_updated: 2026-01-01
---
# Quickstart
EOF
: >wiki/log.md
WIKI_MIDDLEWARE_DIR=$middleware python3 "$middleware/sync.py" . >/dev/null
git add wiki
git commit -qm baseline

# Claude file tools must be denied before they write protected wiki paths.
for blocked in wiki/index.md wiki/log.md wiki/.raw/secret.env; do
  result=$(printf '{"tool_name":"Write","tool_input":{"file_path":"%s"}}' "$blocked" |
    OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" prewrite)
  printf '%s' "$result" | jq -e '.hookSpecificOutput.permissionDecision == "deny"' >/dev/null
done
result=$(printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"wiki/_plan.md"}}' |
  OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" prewrite)
[ -z "$result" ]
result=$(printf '%s' '{"tool_name":"NotebookEdit","tool_input":{"notebook_path":"scratch.ipynb"}}' |
  OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" prewrite)
[ -z "$result" ]

# A shell write is outside pre-tool coverage, but must never auto-commit.
mkdir -p wiki/.raw
printf 'fixture-not-a-secret\n' >wiki/.raw/secret.env
if OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware \
  bash "$hook" autocommit "$vault/wiki/.raw/secret.env" >/dev/null 2>&1; then
  echo "blocked wiki path was committed" >&2
  exit 1
fi
if git cat-file -e HEAD:wiki/.raw/secret.env 2>/dev/null; then
  echo "blocked wiki path appeared in HEAD" >&2
  exit 1
fi
rm wiki/.raw/secret.env
rmdir wiki/.raw

mkdir -p wiki/topic
cat >wiki/topic/new-page.md <<'EOF'
---
type: note
title: New Page
last_updated: 2026-01-02
---
# New Page
EOF
before=$(git rev-list --count HEAD)
OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" autocommit "$vault/wiki/topic/new-page.md"
after=$(git rev-list --count HEAD)
[ "$after" -eq $((before + 1)) ]
[ -z "$(git status --porcelain)" ]
for expected in wiki/topic/new-page.md wiki/topic/index.md wiki/index.md wiki/log.md; do
  git diff-tree --no-commit-id --name-only -r HEAD | grep -Fx "$expected" >/dev/null
done

output=$(OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" autocommit "$vault/wiki/topic/new-page.md")
[ "$output" = clean ]
[ "$(git rev-list --count HEAD)" -eq "$after" ]

printf '\nOpt-out test.\n' >>wiki/topic/new-page.md
mkdir -p .vault-meta
touch .vault-meta/auto-commit.disabled
output=$(OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" autocommit "$vault/wiki/topic/new-page.md")
[ "$output" = disabled ]
[ "$(git rev-list --count HEAD)" -eq "$after" ]
[ -n "$(git status --porcelain -- wiki/topic/new-page.md)" ]
rm .vault-meta/auto-commit.disabled
OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware \
  bash "$hook" autocommit "$vault/wiki/topic/new-page.md" >/dev/null
after=$(git rev-list --count HEAD)
[ -z "$(git status --porcelain -- wiki)" ]

cat >wiki/topic/relative-page.md <<'EOF'
---
type: note
title: Relative Page
last_updated: 2026-01-02
---
# Relative Page
EOF
output=$(
  cd wiki/topic
  printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"relative-page.md"}}' |
    OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" autocommit
)
[ "$output" = committed ]
git diff-tree --no-commit-id --name-only -r HEAD | grep -Fx wiki/topic/relative-page.md >/dev/null
after=$(git rev-list --count HEAD)

cat >wiki/invalid.md <<'EOF'
# Missing frontmatter
EOF
if OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" autocommit "$vault/wiki/invalid.md" >/dev/null 2>&1; then
  echo "invalid page was committed" >&2
  exit 1
fi
[ "$(git rev-list --count HEAD)" -eq "$after" ]
[ -n "$(git status --porcelain -- wiki/invalid.md)" ]
cat >wiki/invalid.md <<'EOF'
---
type: note
title: Repaired
last_updated: 2026-01-03
---
# Repaired
EOF
OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" autocommit "$vault/wiki/invalid.md"
[ -z "$(git status --porcelain -- wiki)" ]

printf 'baseline\n' >unrelated.txt
git add unrelated.txt
git commit -qm fixture
printf 'pre-existing change\n' >>unrelated.txt
cat >wiki/isolated.md <<'EOF'
---
type: note
title: Isolated
last_updated: 2026-01-04
---
# Isolated
EOF
OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" autocommit "$vault/wiki/isolated.md"
[ -z "$(git status --porcelain -- wiki)" ]
[ "$(git status --porcelain -- unrelated.txt)" = ' M unrelated.txt' ]
if git diff --cached --name-only | grep -q .; then
  echo "unrelated changes were staged" >&2
  exit 1
fi

printf '\211PNG\r\n\032\nfixture' >'wiki/topic/diagram 1.png'
OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware \
  bash "$hook" autocommit "$vault/wiki/topic/diagram 1.png" >/dev/null
git diff-tree --no-commit-id --name-only -r HEAD | grep -Fx 'wiki/topic/diagram 1.png' >/dev/null
[ "$(git status --porcelain -- unrelated.txt)" = ' M unrelated.txt' ]
[ -z "$(git diff --cached --name-only)" ]
git restore --worktree -- unrelated.txt

cat >wiki/retry.md <<'EOF'
---
type: note
title: Retry
last_updated: 2026-01-05
---
# Retry
EOF
cat >.git/hooks/pre-commit <<'EOF'
#!/bin/sh
exit 1
EOF
chmod +x .git/hooks/pre-commit
if OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware \
  bash "$hook" autocommit "$vault/wiki/retry.md" >/dev/null 2>&1; then
  echo "rejecting pre-commit hook did not fail autocommit" >&2
  exit 1
fi
[ -n "$(git status --porcelain -- wiki/retry.md)" ]
if git cat-file -e HEAD:wiki/retry.md 2>/dev/null; then
  echo "failed autocommit unexpectedly committed retry page" >&2
  exit 1
fi
[ -z "$(git diff --cached --name-only)" ]
[ "$(git status --porcelain -- wiki | wc -l | tr -d ' ')" -eq 1 ]
rm .git/hooks/pre-commit
OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware \
  bash "$hook" autocommit "$vault/wiki/retry.md" >/dev/null
# OKF §9 log shape: H1 title, YYYY-MM-DD date heading, one prose bullet linking
# the retry page bundle-relative. No legacy `## <datetime> | paths` line.
[ "$(head -1 wiki/log.md)" = '# Directory Update Log' ]
grep -qE '^## [0-9]{4}-[0-9]{2}-[0-9]{2}$' wiki/log.md
[ "$(grep -Fc '(/retry.md)' wiki/log.md)" -eq 1 ]
if grep -qE '^## .*\|' wiki/log.md; then
  echo "log.md still contains a legacy '## <datetime> | paths' heading" >&2
  exit 1
fi
[ -z "$(git status --porcelain)" ]

# Deleted file should be skipped gracefully (not abort the hook).
cat >wiki/ephemeral.md <<'EOF'
---
type: note
title: Ephemeral
last_updated: 2026-01-06
---
# Ephemeral
EOF
rm wiki/ephemeral.md
output=$(OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" autocommit "$vault/wiki/ephemeral.md")
[ "$output" = clean ]

# _plan.md should be skipped (ephemeral planning file, no OKF frontmatter).
cat >wiki/_plan.md <<'EOF'
# Planning notes (no frontmatter)
EOF
output=$(OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" autocommit "$vault/wiki/_plan.md")
[ "$output" = clean ]
rm wiki/_plan.md

# sync.py lists .base (Obsidian Bases) views, so a base-only directory still
# gets a generated index and its parent directory link never dangles.
cat >wiki/topic/board.base <<'EOF'
filters: []
EOF
WIKI_MIDDLEWARE_DIR=$middleware python3 "$middleware/sync.py" . >/dev/null
grep -qx '# Bases' wiki/topic/index.md
grep -Fq '[board](board.base)' wiki/topic/index.md
rm wiki/topic/board.base
WIKI_MIDDLEWARE_DIR=$middleware python3 "$middleware/sync.py" . >/dev/null

mkdir "$work/outside"
output=$(cd "$work/outside" && OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" start)
[ -z "$output" ]

properties=$work/properties.json
printf '{"vaultPath":"%s","features":{"guard":false,"toc":false,"autoCommit":false,"retrievalRefresh":false}}\n' "$vault" >"$properties"
output=$(OBSIDIAN_AGENT_CONFIG=$properties WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" start)
[ -z "$output" ]
output=$(printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"wiki/index.md"}}' |
  OBSIDIAN_AGENT_CONFIG=$properties WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" prewrite)
[ -z "$output" ]
output=$(OBSIDIAN_AGENT_CONFIG=$properties WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" autocommit "$vault/wiki/quickstart.md")
[ "$output" = disabled ]
rm -f "$properties"

# Retrieval helpers are optional and derived state is never staged/committed.
rm -rf scripts .vault-meta
OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" stop
mkdir -p scripts
printf '#!/bin/sh\nexit 1\n' >scripts/contextual-prefix.py
printf '#!/bin/sh\nexit 1\n' >scripts/bm25-index.py
OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" stop 2>"$work/retrieval-warning"
grep -q 'retrieval index refresh failed' "$work/retrieval-warning"
repo=$(cd "$(dirname "$hook")/.." && pwd -P)
cp "$repo/scripts/contextual-prefix.py" scripts/contextual-prefix.py
cp "$repo/scripts/bm25-index.py" scripts/bm25-index.py
OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" stop
[ -f .vault-meta/retrieval/bm25.json ]
[ -z "$(git diff --cached --name-only)" ]
if git cat-file -e HEAD:.vault-meta/retrieval/bm25.json 2>/dev/null; then
  echo "retrieval cache was committed" >&2
  exit 1
fi
# A pre-existing tracked cache is warned about, but remains unstaged.
git add .vault-meta/retrieval/bm25.json
git commit -qm 'tracked retrieval cache fixture'
cat >wiki/cache-fixture.md <<'EOF'
---
type: note
title: Cache Fixture
last_updated: 2026-01-07
---
# Cache Fixture
EOF
WIKI_MIDDLEWARE_DIR=$middleware python3 "$middleware/sync.py" . >/dev/null
# Include page and generated navigation as committed baseline before shutdown.
git add wiki
git commit -qm 'cache fixture page'
OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" stop 2>"$work/tracked-cache-warning"
grep -q 'retrieval cache is tracked' "$work/tracked-cache-warning"
[ -n "$(git diff -- .vault-meta/retrieval/bm25.json)" ]
[ -z "$(git diff --cached --name-only)" ]

# A vault without its own Git repository still syncs indexes and refreshes its
# optional retrieval cache; a containing repository must never be treated as it.
parent=$work/parent-repo
no_git_vault=$parent/nested-vault
mkdir -p "$no_git_vault/wiki" "$no_git_vault/scripts"
git init -q "$parent"
git -C "$parent" config user.name test
git -C "$parent" config user.email test@example.invalid
printf 'parent sentinel\n' >"$parent/sentinel.txt"
git -C "$parent" add sentinel.txt
git -C "$parent" commit -qm baseline
cp "$repo/scripts/contextual-prefix.py" "$no_git_vault/scripts/"
cp "$repo/scripts/bm25-index.py" "$no_git_vault/scripts/"
cat >"$no_git_vault/wiki/no-git-page.md" <<'EOF'
---
type: note
title: No Git Page
last_updated: 2026-01-08
---
# No Git Page
EOF
(cd "$no_git_vault" && OBSIDIAN_VAULT_PATH=$no_git_vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" stop)
[ -f "$no_git_vault/wiki/index.md" ]
[ -f "$no_git_vault/.vault-meta/retrieval/bm25.json" ]
[ ! -e "$no_git_vault/.git" ]
[ -z "$(git -C "$parent" diff --cached --name-only)" ]
[ -z "$(git -C "$parent" diff --name-only)" ]
