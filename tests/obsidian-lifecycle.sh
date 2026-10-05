#!/usr/bin/env bash
set -euo pipefail

hook=${1:?hook path required}
middleware=${2:?middleware path required}
hook=$(cd "$(dirname "$hook")" && pwd -P)/$(basename "$hook")
middleware=$(cd "$middleware" && pwd -P)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
vault=$work/vault
caller=$work/unrelated-repo
mkdir -p "$vault/wiki/topic" "$vault/.obsidian" "$caller"
printf '/.vault-meta/\n' >"$vault/.gitignore"
git -C "$vault" init -q
git -C "$vault" config user.name test
git -C "$vault" config user.email test@example.invalid
git -C "$vault" config core.excludesFile /dev/null
cat >"$vault/wiki/base.md" <<'EOF'
---
type: note
title: Base
last_updated: 2026-01-01
---
# Base
EOF
: >"$vault/wiki/log.md"
WIKI_MIDDLEWARE_DIR=$middleware python3 "$middleware/sync.py" "$vault" >/dev/null
git -C "$vault" add wiki .gitignore
git -C "$vault" commit -qm baseline
base_head=$(git -C "$vault" rev-parse HEAD)

TEST_SESSION=claude-fixture-session
TEST_TOOL_ID=
TEST_TOOL_COUNT=0
capture_with_prewrite() {
  local target=$1 result
  TEST_TOOL_COUNT=$((TEST_TOOL_COUNT + 1))
  TEST_TOOL_ID=claude-fixture-tool-$TEST_TOOL_COUNT
  result=$(cd "$caller" && jq -nc --arg p "$target" --arg session "$TEST_SESSION" --arg id "$TEST_TOOL_ID" '{session_id:$session,tool_use_id:$id,tool_name:"Write",tool_input:{file_path:$p}}' | OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" prewrite)
  [ -z "$result" ]
}

postwrite_captured() {
  (cd "$caller" && OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" postwrite "$1" --owner "claude:$TEST_SESSION" --tool "$TEST_TOOL_ID")
}

stop_session() {
  (cd "$caller" && printf '{"session_id":"%s"}' "$TEST_SESSION" | OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" stop)
}

# A caller outside the vault can create a page with original cwd semantics.
capture_with_prewrite "$vault/wiki/topic/new page.md"
cat >"$vault/wiki/topic/new page.md" <<'EOF'
---
type: note
title: New Page
last_updated: 2026-01-02
---
# New Page
EOF
postwrite_captured "$vault/wiki/topic/new page.md"
[ -f "$vault/.vault-meta/lifecycle/state.json" ]
[ "$(git -C "$vault" rev-parse HEAD)" = "$base_head" ]
[ -z "$(git -C "$vault" diff --cached --name-only)" ]
(cd "$caller" && OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" finalize --owner "claude:$TEST_SESSION")
grep -Fq '[New Page](new%20page.md)' "$vault/wiki/topic/index.md"
grep -Fq '**Creation**' "$vault/wiki/log.md"
[ "$(git -C "$vault" rev-parse HEAD)" = "$base_head" ]

# A different reader's Stop and navigation read cannot consume the writer's
# active prewrite snapshot; writer settlement retains Update classification.
capture_with_prewrite "$vault/wiki/topic/new page.md"
state_before_reader=$(cat "$vault/.vault-meta/lifecycle/state.json")
if (cd "$caller" && printf '{"session_id":"%s","background_tasks":[{"id":"still-running"}],"session_crons":[]}' "$TEST_SESSION" | OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" stop) >/dev/null 2>&1; then
  echo 'Claude Stop recovered while a background task was still active' >&2; exit 1
fi
[ "$(cat "$vault/.vault-meta/lifecycle/state.json")" = "$state_before_reader" ]
if (cd "$caller" && printf '%s' '{"session_id":"reader-fixture","background_tasks":[],"session_crons":[]}' | OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" stop) >/dev/null 2>&1; then
  echo 'reader Stop recovered a foreign writer capture' >&2; exit 1
fi
read_result=$(cd "$caller" && jq -nc --arg p "$vault/wiki/topic/index.md" '{session_id:"reader-fixture",tool_name:"Read",tool_input:{file_path:$p}}' | OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" finalize-read)
printf '%s' "$read_result" | jq -e '.hookSpecificOutput.permissionDecision == "deny"' >/dev/null
[ "$(cat "$vault/.vault-meta/lifecycle/state.json")" = "$state_before_reader" ]
cat >"$vault/wiki/topic/new page.md" <<'EOF'
---
type: note
title: Updated New Page
last_updated: 2026-01-03
---
# Updated New Page
EOF
postwrite_captured "$vault/wiki/topic/new page.md"
(cd "$caller" && OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" finalize --owner "claude:$TEST_SESSION")
grep -Fq '**Update**' "$vault/wiki/log.md"

# Reserved files at any depth/case, traversal, vault settings/source, and unsafe symlinks are denied.
for target in "$vault/wiki/index.md" "$vault/wiki/topic/INDEX.MD" "$vault/wiki/topic/Log.md" \
  "$vault/.raw/secret" "$vault/.obsidian/app.json" "$vault/.vault-meta/cache" "$vault/.git/config" \
  "$vault/wiki/../outside.md"; do
  if (cd "$caller" && OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" guard "$target") >/dev/null 2>&1; then
    echo "protected destination accepted: $target" >&2; exit 1
  fi
done
mkdir -p "$work/outside"
ln -s "$work/outside" "$vault/wiki/escape"
if (cd "$caller" && OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" guard "$vault/wiki/escape/new.md") >/dev/null 2>&1; then
  echo 'unsafe wiki symlink accepted' >&2; exit 1
fi
printf 'ordinary repository write\n' >"$caller/code.py"
(cd "$caller" && OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" guard code.py)

# Invalid changed writes stay pending; later repair allows one coherent publication.
capture_with_prewrite "$vault/wiki/topic/invalid.md"
cat >"$vault/wiki/topic/invalid.md" <<'EOF'
# missing frontmatter
EOF
if postwrite_captured "$vault/wiki/topic/invalid.md" >/dev/null 2>&1; then
  echo 'invalid changed page accepted' >&2; exit 1
fi
index_before=$(shasum -a 256 "$vault/wiki/index.md" | awk '{print $1}')
if (cd "$caller" && OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" finalize --owner "claude:$TEST_SESSION") >/dev/null 2>&1; then
  echo 'invalid batch finalized' >&2; exit 1
fi
read_result=$(cd "$caller" && jq -nc --arg p "$vault/wiki/topic/index.md" --arg session "$TEST_SESSION" '{session_id:$session,tool_name:"Read",tool_input:{file_path:$p}}' | OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" finalize-read)
printf '%s' "$read_result" | jq -e '.hookSpecificOutput.permissionDecision == "deny"' >/dev/null
read_result=$(cd "$caller" && jq -nc --arg p "$caller/code.py" --arg session "$TEST_SESSION" '{session_id:$session,tool_name:"Read",tool_input:{file_path:$p}}' | OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" finalize-read)
[ -z "$read_result" ]
[ "$(shasum -a 256 "$vault/wiki/index.md" | awk '{print $1}')" = "$index_before" ]
cat >"$vault/wiki/topic/invalid.md" <<'EOF'
---
type: note
title: Repaired
---
# Repaired
EOF
postwrite_captured "$vault/wiki/topic/invalid.md"
stop_session
grep -Fq '[Repaired](invalid.md)' "$vault/wiki/topic/index.md"

# No-op and retry do not duplicate log entries; unrelated Git state is untouched.
cat >"$vault/unrelated.txt" <<'EOF'
base
EOF
git -C "$vault" add unrelated.txt
git -C "$vault" commit -qm external
printf 'dirty unrelated\n' >>"$vault/unrelated.txt"
git -C "$vault" add unrelated.txt
staged_before=$(git -C "$vault" diff --cached --name-only)
stop_session
[ "$(git -C "$vault" diff --cached --name-only)" = "$staged_before" ]
[ "$(git -C "$vault" status --porcelain -- unrelated.txt | cut -c 4-)" = unrelated.txt ]
[ "$(grep -Fc '(/topic/invalid.md)' "$vault/wiki/log.md")" -eq 1 ]

# Legacy autoCommit=true cannot commit, stage, reset, or disturb unrelated staged data.
properties=$work/auto-commit-true.json
printf '{"vaultPath":"%s","features":{"autoCommit":true,"guard":true}}\n' "$vault" >"$properties"
TEST_TOOL_ID=claude-fixture-tool-auto
result=$(cd "$caller" && jq -nc --arg p "$vault/wiki/topic/auto-commit-legacy.md" --arg session "$TEST_SESSION" --arg id "$TEST_TOOL_ID" '{session_id:$session,tool_use_id:$id,tool_name:"Write",tool_input:{file_path:$p}}' | OBSIDIAN_AGENT_CONFIG=$properties OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" prewrite)
[ -z "$result" ]
cat >"$vault/wiki/topic/auto-commit-legacy.md" <<'EOF'
---
type: note
title: Legacy Auto Commit
---
# Legacy Auto Commit
EOF
(cd "$caller" && OBSIDIAN_AGENT_CONFIG=$properties OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" postwrite "$vault/wiki/topic/auto-commit-legacy.md" --owner "claude:$TEST_SESSION" --tool "$TEST_TOOL_ID")
staged_before=$(git -C "$vault" diff --cached --name-only)
head_before=$(git -C "$vault" rev-parse HEAD)
(cd "$caller" && OBSIDIAN_AGENT_CONFIG=$properties OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" finalize --owner "claude:$TEST_SESSION")
[ "$(git -C "$vault" diff --cached --name-only)" = "$staged_before" ]
[ "$(git -C "$vault" rev-parse HEAD)" = "$head_before" ]

# Startup is a locator-only operation from an unrelated cwd.
head_before=$(git -C "$vault" rev-parse HEAD)
output=$(cd "$caller" && OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" start 2>"$work/start-warning")
printf '%s' "$output" | grep -F "$vault/wiki" >/dev/null
[ "$(git -C "$vault" rev-parse HEAD)" = "$head_before" ]

# Unrelated read-only startup/shutdown in a no-Git vault must not create runtime state.
plain=$work/plain-vault
mkdir -p "$plain/wiki"
printf '# Plain Wiki\n' >"$plain/wiki/page.md"
(cd "$caller" && OBSIDIAN_VAULT_PATH=$plain WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" start >/dev/null 2>"$work/plain-start-warning")
(cd "$caller" && OBSIDIAN_VAULT_PATH=$plain WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" stop)
[ ! -e "$plain/.vault-meta" ]
[ ! -e "$plain/.git" ]
plain_without_wiki=$work/selected-vault-without-wiki
mkdir -p "$plain_without_wiki"
result=$(cd "$caller" && printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"code.py"}}' | OBSIDIAN_VAULT_PATH=$plain_without_wiki WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" prewrite)
[ -z "$result" ]
[ ! -e "$plain_without_wiki/.vault-meta" ]

# Malformed integration config blocks selected wiki writes but not unrelated repo writes.
properties=$work/bad-properties.json
printf '{"vaultPath":"%s","features":{"guard":"bad"}}\n' "$vault" >"$properties"
result=$(cd "$caller" && printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"code.py"}}' | OBSIDIAN_AGENT_CONFIG=$properties OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" prewrite)
[ -z "$result" ]
result=$(cd "$caller" && printf '%s' "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$vault/wiki/base.md\"}}" | OBSIDIAN_AGENT_CONFIG=$properties OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" prewrite)
printf '%s' "$result" | jq -e '.hookSpecificOutput.permissionDecision == "deny"' >/dev/null
read_result=$(cd "$caller" && jq -nc --arg p "$caller/code.py" '{tool_name:"Read",tool_input:{file_path:$p}}' | OBSIDIAN_AGENT_CONFIG=$properties OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" finalize-read)
[ -z "$read_result" ]
read_result=$(cd "$caller" && jq -nc --arg p "$vault/wiki/index.md" '{tool_name:"Read",tool_input:{file_path:$p}}' | OBSIDIAN_AGENT_CONFIG=$properties OBSIDIAN_VAULT_PATH=$vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" finalize-read)
printf '%s' "$read_result" | jq -e '.hookSpecificOutput.permissionDecision == "deny"' >/dev/null
printf '{invalid json\\n' >"$work/unparseable-properties.json"
result=$(cd "$caller" && printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"code.py"}}' | OBSIDIAN_AGENT_CONFIG=$work/unparseable-properties.json WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" prewrite 2>/dev/null)
[ -z "$result" ]

# Unsafe state storage blocks selected wiki writes with an explicit hook denial,
# while an ordinary caller-repository write remains unaffected.
broken_state_vault=$work/broken-state-vault
mkdir -p "$broken_state_vault/wiki" "$work/external-state"
ln -s "$work/external-state" "$broken_state_vault/.vault-meta"
result=$(cd "$caller" && jq -nc --arg p "$broken_state_vault/wiki/new.md" --arg session "$TEST_SESSION" --arg id broken-state '{session_id:$session,tool_use_id:$id,tool_name:"Write",tool_input:{file_path:$p}}' | OBSIDIAN_VAULT_PATH=$broken_state_vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" prewrite)
printf '%s' "$result" | jq -e '.hookSpecificOutput.permissionDecision == "deny" and (.hookSpecificOutput.permissionDecisionReason | contains("prewrite state could not be captured"))' >/dev/null
result=$(cd "$caller" && jq -nc --arg p "$caller/code.py" '{tool_name:"Write",tool_input:{file_path:$p}}' | OBSIDIAN_VAULT_PATH=$broken_state_vault WIKI_MIDDLEWARE_DIR=$middleware bash "$hook" prewrite)
[ -z "$result" ]
[ -z "$(find "$work/external-state" -mindepth 1 -print -quit)" ]

printf 'vault lifecycle checks PASS\n'
