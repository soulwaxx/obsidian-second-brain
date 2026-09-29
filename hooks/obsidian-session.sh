#!/usr/bin/env bash
# Shared Claude/pi lifecycle for the configured Obsidian wiki vault.
set -uo pipefail

cmd="${1:-}"
shift || true

canonical_dir() {
  (cd -P -- "$1" 2>/dev/null && pwd -P)
}

properties=${OBSIDIAN_AGENT_CONFIG:-$HOME/.config/obsidian-second-brain/properties.json}
if [ -f "$properties" ]; then
  command -v jq >/dev/null 2>&1 || exit 2
  jq -e 'type == "object" and ((.vaultPath? == null) or (.vaultPath | type == "string")) and ((.features? == null) or (.features | type == "object"))' "$properties" >/dev/null || exit 2
  if [ -z "${OBSIDIAN_VAULT_PATH:-}" ]; then
    OBSIDIAN_VAULT_PATH=$(jq -r '.vaultPath // empty' "$properties") || exit 2
  fi
fi

feature_enabled() {
  [ ! -f "$properties" ] || [ "$(jq -r --arg name "$1" 'if .features[$name] == null then true else .features[$name] end' "$properties")" = true ]
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
  guard_path "${1:-}"
  ;;

prewrite)
  feature_enabled guard || exit 0
  command -v jq >/dev/null 2>&1 || exit 2
  input=$(cat)
  tool=$(printf '%s' "$input" | jq -er '.tool_name // empty') || exit 2
  case "$tool" in Edit|MultiEdit|NotebookEdit|Write) ;; *) exit 0 ;; esac
  raw=$(printf '%s' "$input" | jq -er '.tool_input.file_path // .tool_input.notebook_path // empty') || exit 2
  if ! reason=$(guard_path "$raw" 2>&1); then
    jq -n --arg reason "$reason" \
      '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
  fi
  ;;

start)
  if feature_enabled toc; then
    [ -f wiki/index.md ] && cat wiki/index.md
  fi
  [ -x scripts/wiki-lock.sh ] && bash scripts/wiki-lock.sh clear-stale --max-age 3600 >/dev/null 2>&1
  true
  ;;

autocommit)
  if ! feature_enabled autoCommit; then
    echo disabled
    exit 0
  fi
  [ -d .git ] || exit 0
  if [ -f .vault-meta/auto-commit.disabled ]; then
    echo disabled
    exit 0
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

  # A dirty, unrelated wiki path can alter generated indexes. Refuse rather
  # than fold that pre-existing edit into this page's commit.
  while IFS= read -r -d '' dirty; do
    dirty=${dirty:3}
    owned=0
    for page in "${changed[@]}"; do
      [ "$dirty" = "$page" ] && owned=1
    done
    if [ "$owned" = 0 ]; then
      echo "obsidian lifecycle: unrelated dirty wiki path prevents isolated commit: $dirty" >&2
      exit 1
    fi
  done < <(git status --porcelain=v1 -z --untracked-files=all -- wiki)

  mw=$(middleware_dir)
  [ -n "$mw" ] && [ -f "$mw/validate.py" ] && [ -f "$mw/sync.py" ] || {
    echo "obsidian lifecycle: wiki middleware is unavailable" >&2
    exit 1
  }
  for touched_path in "${changed[@]}"; do
    case "$touched_path" in
      *.[mM][dD]) ;;
      *) continue ;;
    esac
    if ! python3 "$mw/validate.py" "$touched_path" >/dev/null; then
      echo "obsidian lifecycle: validation failed for $touched_path; repair it before commit" >&2
      exit 1
    fi
  done
  python3 "$mw/sync.py" . >/dev/null || {
    echo "obsidian lifecycle: index synchronization failed" >&2
    exit 1
  }

  generated=()
  while IFS= read -r -d '' index; do
    duplicate=0
    for existing in "${generated[@]+"${generated[@]}"}"; do
      [ "$existing" = "$index" ] && duplicate=1
    done
    [ "$duplicate" = 1 ] || generated+=("$index")
  done < <(git diff --name-only -z -- 'wiki/**/index.md' wiki/index.md)
  while IFS= read -r -d '' index; do
    duplicate=0
    for existing in "${generated[@]+"${generated[@]}"}"; do
      [ "$existing" = "$index" ] && duplicate=1
    done
    [ "$duplicate" = 1 ] || generated+=("$index")
  done < <(git ls-files --others --exclude-standard -z -- 'wiki/**/index.md' wiki/index.md)

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

  commit_paths=("${changed[@]}" "${generated[@]+"${generated[@]}"}" wiki/log.md)
  cleanup_failed_commit() {
    local hook_path cleanup_failed=0
    git reset -q HEAD -- "${commit_paths[@]}" || cleanup_failed=1
    for hook_path in "${generated[@]+"${generated[@]}"}" wiki/log.md; do
      if git ls-files --error-unmatch -- "$hook_path" >/dev/null 2>&1; then
        git restore --worktree --source=HEAD -- "$hook_path" || cleanup_failed=1
      else
        rm -f -- "$hook_path" || cleanup_failed=1
      fi
    done
    [ "$cleanup_failed" = 0 ] && git diff --cached --quiet
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
  echo committed
  ;;

stop)
  [ -d wiki ] || exit 0
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
    python3 "$mw/sync.py" . >/dev/null || {
      echo "obsidian lifecycle: shutdown index synchronization failed" >&2
      exit 1
    }
    if [ "$has_vault_git" = 1 ] && [ -n "$(git status --porcelain -- 'wiki/**/index.md' wiki/index.md)" ]; then
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
  echo "usage: obsidian-session.sh {start|autocommit|stop} [touched-page ...]" >&2
  exit 2
  ;;
esac
