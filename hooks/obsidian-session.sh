#!/usr/bin/env bash
# Shared Claude/pi lifecycle for the configured Obsidian wiki vault.
set -uo pipefail

cmd="${1:-}"
shift || true

canonical_dir() {
  (cd -P -- "$1" 2>/dev/null && pwd -P)
}

properties=${OBSIDIAN_AGENT_CONFIG:-$HOME/.config/obsidian-second-brain/properties.json}
script_root=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
config_data='{}'
config_error=
explicit_override=${OBSIDIAN_VAULT_PATH:-}
if [ -f "$properties" ]; then
  command -v jq >/dev/null 2>&1 || exit 2
  contract=$(python3 "$script_root/scripts/config_contract.py" "$properties") || exit 2
  config_data=$(printf '%s' "$contract" | jq -c '.config // {}')
  config_error=$(printf '%s' "$contract" | jq -r '.error // empty')
  if [ -z "$explicit_override" ]; then
    OBSIDIAN_VAULT_PATH=$(printf '%s' "$contract" | jq -r 'if (.config.vaultPath | type) == "string" then .config.vaultPath else empty end')
  fi
  if [ -n "$config_error" ] && [ -z "$explicit_override" ] && [ -z "${OBSIDIAN_VAULT_PATH:-}" ]; then
    echo "obsidian lifecycle: $config_error" >&2
    exit 2
  fi
  [ -z "$config_error" ] || echo "obsidian lifecycle: $config_error; retaining explicit vault boundary" >&2
fi

feature_enabled() {
  if [ -n "$config_error" ]; then
    return 1
  fi
  [ "$(printf '%s' "$config_data" | jq -r --arg name "$1" 'if .features[$name] == null then true else .features[$name] end')" = true ]
}

vault=$(canonical_dir "${OBSIDIAN_VAULT_PATH:-}") || exit 0
invocation_cwd=$(pwd -P)
case "$invocation_cwd/" in
  "$vault"/*) ;;
  *) exit 0 ;;
esac
cd -- "$vault" || exit 1

middleware_dir() {
  if [ -n "${WIKI_MIDDLEWARE_DIR:-}" ] && [ -d "$WIKI_MIDDLEWARE_DIR" ]; then
    printf '%s\n' "$WIKI_MIDDLEWARE_DIR"
    return
  fi
  local cand root
  root=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
  for cand in "$root/skills/wiki/scripts/okf_mw"; do
    if [ -d "$cand" ]; then
      printf '%s\n' "$cand"
      return
    fi
  done
}

guard_path() {
  local target=${1:?path required} mw
  [[ "$target" = /* ]] || target="$invocation_cwd/$target"
  mw=$(middleware_dir)
  [ -n "$mw" ] && [ -f "$mw/guard.py" ] || {
    echo "obsidian lifecycle: wiki write guard is unavailable" >&2
    return 1
  }
  python3 "$mw/guard.py" --if-wiki "$vault" "$target"
}

validate_publishable_pages() {
  local mw rel validation_output
  mw=$(middleware_dir)
  [ -n "$mw" ] && [ -f "$mw/validate.py" ] || {
    echo "obsidian lifecycle: wiki validation middleware is unavailable" >&2
    return 1
  }
  while IFS= read -r -d '' rel; do
    if validation_output=$(python3 "$mw/validate.py" "$rel" 2>&1); then
      continue
    fi
    printf 'obsidian lifecycle: validation failed for %s; repair or remove it before navigation refresh\n' "$rel" >&2
    [ -z "$validation_output" ] || printf '%s\n' "$validation_output" >&2
    return 1
  done < <(python3 -c '
import os
import sys

root = sys.argv[1]
skip = {"index.md", "log.md", "_plan.md", "instructions.md"}

def walk(directory, relative):
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(".") or entry.is_symlink():
            continue
        path = os.path.join(directory, entry.name)
        rel = os.path.join(relative, entry.name)
        if entry.is_dir(follow_symlinks=False):
            yield from walk(path, rel)
        elif (entry.is_file(follow_symlinks=False)
              and entry.name.lower().endswith(".md")
              and entry.name.casefold() not in skip):
            yield rel

for page in walk(root, "wiki"):
    sys.stdout.buffer.write(os.fsencode(page) + b"\0")
' "$vault/wiki")
}

vault_git_root() {
  local root
  root=$(git rev-parse --show-toplevel 2>/dev/null) || return 1
  root=$(canonical_dir "$root") || return 1
  [ "$root" = "$vault" ] || return 1
}

acquire_lock() {
  local git_lock
  git_lock=$(git rev-parse --git-path obsidian-lifecycle.lock 2>/dev/null) || return 1
  lifecycle_lock=$git_lock
  if ! mkdir "$lifecycle_lock" 2>/dev/null; then
    holder=$(cat "$lifecycle_lock/pid" 2>/dev/null || true)
    if [[ "$holder" =~ ^[0-9]+$ ]] && ! kill -0 "$holder" 2>/dev/null; then
      rm -rf -- "$lifecycle_lock"
      mkdir "$lifecycle_lock" 2>/dev/null || return 1
    else
      echo "obsidian lifecycle: another commit is in progress" >&2
      return 1
    fi
  fi
  printf '%s\n' "$$" >"$lifecycle_lock/pid"
  trap 'rm -rf -- "$lifecycle_lock"' EXIT HUP INT TERM
}

case "$cmd" in
guard)
  if [ -n "$config_error" ]; then
    echo "obsidian lifecycle: $config_error; repair the selected integration config with explicit approval before wiki writes" >&2
    exit 1
  fi
  guard_path "${1:-}"
  ;;

prewrite)
  if [ -z "$config_error" ]; then feature_enabled guard || exit 0; fi
  command -v jq >/dev/null 2>&1 || exit 2
  input=$(cat)
  tool=$(printf '%s' "$input" | jq -er '.tool_name // empty') || exit 2
  case "$tool" in Edit|MultiEdit|NotebookEdit|Write) ;; *) exit 0 ;; esac
  raw=$(printf '%s' "$input" | jq -er '.tool_input.file_path // .tool_input.notebook_path // empty') || exit 2
  if [ -n "$config_error" ]; then
    repair=$(python3 "$script_root/scripts/config_contract.py" "$properties" --repair-target "$raw" --cwd "$invocation_cwd" | jq -r '.repairTarget // false') || repair=false
    if [ "$repair" = true ]; then exit 0; fi
    jq -n --arg reason "Obsidian config is invalid; only the selected config file may be repaired before resuming writes" \
      '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
    exit 0
  fi
  if ! reason=$(guard_path "$raw" 2>&1); then
    jq -n --arg reason "$reason" \
      '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
  fi
  ;;

start)
  if [ -n "$config_error" ]; then
    printf 'Obsidian integration configuration needs repair: %s\n' "$config_error"
    printf 'Stop wiki writes. Propose the exact minimal change to the selected integration config, preserve unrelated fields, and obtain explicit approval for those changes. Edit only that config; revalidate it in this session before resuming. Do not reset settings or enable autoCommit.\n'
  fi
  if feature_enabled toc; then
    [ -f wiki/index.md ] && cat wiki/index.md
  fi
  [ -x scripts/wiki-lock.sh ] && bash scripts/wiki-lock.sh clear-stale --max-age 3600 >/dev/null 2>&1
  true
  ;;

postwrite|autocommit)
  if [ -n "$config_error" ]; then
    echo "obsidian lifecycle: $config_error; refusing wiki synchronization until config repair and revalidation" >&2
    exit 1
  fi
  requested=("$@")
  if [ ${#requested[@]} -eq 0 ] && command -v jq >/dev/null 2>&1; then
    input=$(cat)
    tool_ok=$(printf '%s' "$input" | jq -er '.tool_name | . == "Edit" or . == "MultiEdit" or . == "NotebookEdit" or . == "Write"' 2>/dev/null) || {
      echo "obsidian lifecycle: malformed post-tool input" >&2
      exit 1
    }
    [ "$tool_ok" = true ] || exit 0
    file_path=$(printf '%s' "$input" | jq -er '.tool_input.file_path // .tool_input.notebook_path // ""' 2>/dev/null) || {
      echo "obsidian lifecycle: malformed post-tool path" >&2
      exit 1
    }
    [ -n "$file_path" ] && requested+=("$file_path")
  fi
  [ ${#requested[@]} -gt 0 ] || exit 0

  touched=()
  for raw in "${requested[@]}"; do
    guard_path "$raw" >/dev/null || exit 1
    canonical=
    if ! IFS= read -r -d '' canonical < <(python3 - "$invocation_cwd" "$raw" <<'PY'
import os
from pathlib import Path
import sys
try:
    raw = Path(sys.argv[2])
    target = (raw if raw.is_absolute() else Path(sys.argv[1]) / raw).resolve(strict=True)
    if not target.is_file():
        raise OSError
    sys.stdout.buffer.write(os.fsencode(target) + b"\0")
except (OSError, RuntimeError):
    raise SystemExit(1)
PY
); then
      # File was deleted or is not a regular file — skip gracefully.
      # Common case: wiki skill creates _plan.md then deletes it before
      # turn_end fires, or the agent removes an ephemeral file mid-turn.
      continue
    fi
    case "$canonical" in
      "$vault"/wiki/*) ;;
      *) continue ;;
    esac
    case "${canonical##*/}" in
      [iI][nN][dD][eE][xX].[mM][dD]|[lL][oO][gG].[mM][dD]|_[pP][lL][aA][nN].[mM][dD]) continue ;;
    esac
    duplicate=0
    # macOS Bash 3 treats an empty array expansion as unset under nounset.
    for existing in "${touched[@]+"${touched[@]}"}"; do
      [ "$existing" = "$canonical" ] && duplicate=1
    done
    [ "$duplicate" = 1 ] || touched+=("$canonical")
  done
  if [ ${#touched[@]} -eq 0 ]; then
    echo clean
    exit 0
  fi

  mw=$(middleware_dir)
  [ -n "$mw" ] && [ -f "$mw/validate.py" ] && [ -f "$mw/sync.py" ] || {
    echo "obsidian lifecycle: wiki middleware is unavailable" >&2
    exit 1
  }
  validate_publishable_pages || exit 1
  sync_output=$(python3 "$mw/sync.py" . --json)
  sync_status=$?
  if [ "$sync_status" -ne 0 ]; then
    sync_errors=$(printf '%s\n' "$sync_output" | jq -r '.results[]? | select(.status == "ERROR") | "obsidian lifecycle: " + .path + ": " + .detail' 2>/dev/null) || sync_errors=
    if [ -n "$sync_errors" ]; then
      printf '%s\n' "$sync_errors" >&2
    else
      printf 'obsidian lifecycle: index synchronization failed; middleware output follows:\n%s\n' "$sync_output" >&2
    fi
    exit 1
  fi
  if [ "$cmd" = postwrite ]; then
    echo synced
    exit 0
  fi

  if ! feature_enabled autoCommit || [ -f .vault-meta/auto-commit.disabled ]; then
    echo disabled
    exit 0
  fi
  [ -d .git ] || { echo clean; exit 0; }
  vault_git_root || { echo clean; exit 0; }

  changed=()
  for touched_path in "${touched[@]}"; do
    rel=${touched_path#"$vault"/}
    if [ -n "$(git status --porcelain -- "$rel")" ]; then
      changed+=("$rel")
    fi
  done
  if [ ${#changed[@]} -eq 0 ]; then
    echo clean
    exit 0
  fi

  acquire_lock || exit 1
  if ! git diff --cached --quiet; then
    echo "obsidian lifecycle: staged changes already exist; refusing unsafe auto-commit" >&2
    exit 1
  fi
  if [ -x scripts/wiki-lock.sh ]; then
    lock_list=$(bash scripts/wiki-lock.sh list 2>/dev/null) || {
      echo "obsidian lifecycle: wiki-lock list failed" >&2
      exit 1
    }
    if [ -n "$lock_list" ]; then
      echo "obsidian lifecycle: wiki page lock is held; deferring auto-commit" >&2
      exit 1
    fi
  fi

  ownership_helper="$mw/ownership.py"
  [ -f "$ownership_helper" ] || {
    echo "obsidian lifecycle: generated-index ownership verifier is unavailable" >&2
    exit 1
  }
  log_ownership=$(python3 "$ownership_helper" capture "$vault" wiki/log.md) || {
    echo "obsidian lifecycle: wiki/log.md has unowned dirty bytes; preserve and review it" >&2
    exit 1
  }
  log_before=$(printf '%s' "$log_ownership" | jq -r '.before // "null"')
  log_clean=$(printf '%s' "$log_ownership" | jq -r '.clean')

  # Refuse unrelated dirty pages, but leave generated indexes to the exact
  # directories affected by this page lifecycle (and their ancestors).
  while IFS= read -r -d '' dirty; do
    dirty=${dirty:3}
    owned=0
    case "${dirty##*/}" in [iI][nN][dD][eE][xX].[mM][dD]) owned=1 ;; esac
    [ "$dirty" = wiki/log.md ] && owned=1
    for page in "${changed[@]}"; do
      [ "$dirty" = "$page" ] && owned=1
    done
    if [ "$owned" = 0 ]; then
      echo "obsidian lifecycle: unrelated dirty wiki path prevents isolated commit: $dirty" >&2
      exit 1
    fi
  done < <(git status --porcelain=v1 -z --untracked-files=all -- wiki)

  generated=()
  index_relevant() {
    local index_dir page_dir page
    index_dir=${1%/index.md}
    [ -z "$index_dir" ] && index_dir=wiki
    for page in "${changed[@]}"; do
      page_dir=${page%/*}
      while [ "$page_dir" != . ]; do
        [ "$page_dir" = "$index_dir" ] && return 0
        [ "$page_dir" = wiki ] && break
        page_dir=${page_dir%/*}
      done
      [ "$index_dir" = wiki ] && return 0
    done
    return 1
  }
  owned_indexes=$(python3 "$ownership_helper" list "$vault") || {
    echo "obsidian lifecycle: generated-index ownership journal is invalid" >&2
    exit 1
  }
  while IFS= read -r index; do
    [ -n "$index" ] || continue
    if index_relevant "$index"; then
      generated+=("$index")
    fi
  done < <(printf '%s' "$owned_indexes" | jq -r '.[].path')

  # OKF v0.2 §9 log format: an H1 title, `## YYYY-MM-DD` date headings newest
  # first, and prose bullets whose leading bold word is the change kind. Each
  # changed path is classified Creation (absent from HEAD) or Update, and
  # linked bundle-relative (`/<path-without-wiki-prefix>`). Same-day runs merge
  # under one date heading instead of stacking a heading per commit.
  today=$(date -u '+%Y-%m-%d')
  log_entries=()
  for rel in "${changed[@]}"; do
    if git cat-file -e "HEAD:$rel" 2>/dev/null; then
      verb=Update
    else
      verb=Creation
    fi
    log_entries+=("$verb"$'\t'"${rel#wiki/}")
  done
  if ! python3 - "$today" wiki/log.md "${log_entries[@]}" >wiki/log.md.tmp <<'PY'
import re
import sys
import urllib.parse

today = sys.argv[1]
log_path = sys.argv[2]
TITLE = "# Directory Update Log"
PROSE = {"Creation": "Added", "Update": "Revised"}

bullets = []
for line in sys.argv[3:]:
    verb, _, rel = line.partition("\t")
    if not rel:
        continue
    href = "/" + urllib.parse.quote(rel)
    bullets.append(f"* **{verb}**: {PROSE.get(verb, 'Changed')} [{rel}]({href}).")
if not bullets:
    raise SystemExit(1)
new_bullets = "\n".join(bullets)

try:
    with open(log_path, encoding="utf-8") as fh:
        existing = fh.read()
except FileNotFoundError:
    existing = ""

lines = existing.lstrip("\ufeff").splitlines()
idx = 0
while idx < len(lines) and lines[idx].strip() == "":
    idx += 1
if idx < len(lines) and lines[idx].strip() == TITLE:
    idx += 1
rest = "\n".join(lines[idx:]).strip("\n")

first = re.match(r"^##\s+(.*)", rest)
if first and first.group(1).strip() == today:
    newline = rest.find("\n")
    head_line = rest if newline == -1 else rest[:newline]
    section_body = ("" if newline == -1 else rest[newline + 1:]).lstrip("\n")
    repeated = set(bullets)
    section_lines = [line for line in section_body.splitlines() if line not in repeated]
    section_body = "\n".join(section_lines)
    rest = head_line + "\n" + new_bullets + ("\n" + section_body if section_body else "")
else:
    section = f"## {today}\n{new_bullets}"
    rest = section + ("\n\n" + rest if rest.strip() else "")

sys.stdout.write(TITLE + "\n\n" + rest.strip("\n") + "\n")
PY
  then
    rm -f wiki/log.md.tmp
    echo "obsidian lifecycle: log update failed" >&2
    exit 1
  fi
  mv wiki/log.md.tmp wiki/log.md
  python3 "$ownership_helper" record-file "$vault" wiki/log.md "$log_clean" "$log_before" wiki/log.md || {
    echo "obsidian lifecycle: could not record wiki/log.md ownership; preserve pending bytes" >&2
    exit 1
  }

  commit_paths=("${changed[@]}" "${generated[@]+"${generated[@]}"}" wiki/log.md)
  cleanup_failed_commit() {
    # Only unstage this attempted commit. Keep the validated page, verified
    # generated index bytes, and lifecycle log intact for a safe retry.
    git reset -q HEAD -- "${commit_paths[@]}" && git diff --cached --quiet
  }
  if ! git add -- "${commit_paths[@]}"; then
    cleanup_failed_commit || echo "obsidian lifecycle: failed to clean hook-owned index state" >&2
    echo "obsidian lifecycle: staging failed; touched paths retained" >&2
    exit 1
  fi
  if ! git commit -m "wiki: auto-commit $(date '+%Y-%m-%d %H:%M')" -- "${commit_paths[@]}" >/dev/null; then
    cleanup_failed_commit || echo "obsidian lifecycle: failed to clean hook-owned index state" >&2
    echo "obsidian lifecycle: commit failed; touched paths retained" >&2
    exit 1
  fi
  if [ -n "$(git status --porcelain -- "${commit_paths[@]}")" ]; then
    echo "obsidian lifecycle: committed paths remain dirty" >&2
    exit 1
  fi
  python3 "$ownership_helper" forget "$vault" "${generated[@]+"${generated[@]}"}" wiki/log.md >/dev/null ||
    echo "obsidian lifecycle: committed, but ownership-journal cleanup needs review" >&2
  echo committed
  ;;

stop)
  [ -d wiki ] || exit 0
  if [ -n "$config_error" ]; then
    echo "obsidian lifecycle: $config_error; refusing shutdown synchronization until config repair and revalidation" >&2
    exit 1
  fi
  has_vault_git=0
  if vault_git_root; then
    has_vault_git=1
    acquire_lock || exit 1
    if ! git diff --cached --quiet; then
      echo "obsidian lifecycle: staged changes already exist; refusing unsafe shutdown commit" >&2
      exit 1
    fi
  fi
  mw=$(middleware_dir)
  if [ -n "$mw" ] && [ -f "$mw/sync.py" ]; then
    validate_publishable_pages || exit 1
    shutdown_sync_output=$(python3 "$mw/sync.py" . --json)
    shutdown_sync_status=$?
    if [ "$shutdown_sync_status" -ne 0 ]; then
      shutdown_sync_errors=$(printf '%s\n' "$shutdown_sync_output" | jq -r '.results[]? | select(.status == "ERROR") | "obsidian lifecycle: " + .path + ": " + .detail' 2>/dev/null) || shutdown_sync_errors=
      if [ -n "$shutdown_sync_errors" ]; then
        printf '%s\n' "$shutdown_sync_errors" >&2
      else
        printf 'obsidian lifecycle: shutdown index synchronization failed; middleware output follows:\n%s\n' "$shutdown_sync_output" >&2
      fi
      exit 1
    fi
    if [ "$has_vault_git" = 1 ] && feature_enabled autoCommit && [ ! -f .vault-meta/auto-commit.disabled ] &&
      [ -n "$(git status --porcelain -- 'wiki/**/index.md' wiki/index.md)" ]; then
      echo "obsidian lifecycle: generated indexes drifted at shutdown; repair and commit with the page change" >&2
      exit 1
    fi
  fi

  if feature_enabled retrievalRefresh && [ -f scripts/contextual-prefix.py ] && [ -f scripts/bm25-index.py ]; then
    if [ "$has_vault_git" = 1 ] && [ -n "$(git ls-files -- .vault-meta/retrieval/)" ]; then
      echo "obsidian lifecycle: warning: retrieval cache is tracked; keep derived state uncommitted" >&2
    fi
    if ! python3 scripts/contextual-prefix.py --all >/dev/null ||
      ! python3 scripts/bm25-index.py build >/dev/null; then
      echo "obsidian lifecycle: warning: retrieval index refresh failed; wiki index navigation remains available" >&2
    fi
  fi
  if [ -n "$mw" ] && [ -f "$mw/lint.py" ]; then
    python3 "$mw/lint.py" . 2>/dev/null | python3 -c '
import json, sys
try:
    report = json.load(sys.stdin)
except Exception:
    sys.exit(0)
counts = {k: len(v) for k, v in report.items() if isinstance(v, list) and v}
if counts:
    print("wiki lint: " + ", ".join(f"{k}={n}" for k, n in sorted(counts.items())))
'
  fi
  ;;

*)
  echo "usage: obsidian-session.sh {start|postwrite|autocommit|stop} [touched-page ...]" >&2
  exit 2
  ;;
esac
