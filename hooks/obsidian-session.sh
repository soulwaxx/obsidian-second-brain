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

is_selected_wiki_target() {
  local target=${1:?path required}
  [[ "$target" = /* ]] || target="$invocation_cwd/$target"
  python3 - "$vault" "$target" <<'PY'
import os, sys
vault, target = map(os.path.realpath, sys.argv[1:])
try:
    inside = os.path.commonpath([os.path.join(vault, "wiki"), target]) == os.path.join(vault, "wiki")
except ValueError:
    inside = False
raise SystemExit(0 if inside else 1)
PY
}

claude_owner() {
  jq -r 'if (.session_id // "") == "" then "" else ("claude:" + .session_id + (if (.agent_id // "") != "" then ":" + .agent_id else "" end)) end'
}

deny_prewrite() {
  jq -n --arg reason "$1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
}

case "$cmd" in
guard)
  guard_path "${1:-}"
  ;;

prewrite)
  guard_enabled=1
  if [ -z "$config_error" ] && ! feature_enabled guard; then guard_enabled=0; fi
  command -v jq >/dev/null 2>&1 || exit 2
  input=$(cat)
  tool=$(printf '%s' "$input" | jq -er '.tool_name // empty') || exit 2
  case "$tool" in Edit|MultiEdit|NotebookEdit|Write) ;; *) exit 0 ;; esac
  raw=$(printf '%s' "$input" | jq -er '.tool_input.file_path // .tool_input.notebook_path // empty') || exit 2
  owner=$(printf '%s' "$input" | claude_owner)
  tool_id=$(printf '%s' "$input" | jq -r '.tool_use_id // empty')
  if [ -n "$config_error" ]; then
    repair=$(python3 "$script_root/scripts/config_contract.py" "$properties" --repair-target "$raw" --cwd "$invocation_cwd" | jq -r '.repairTarget // false') || repair=false
    if [ "$repair" = true ]; then exit 0; fi
    target=$raw
    [[ "$target" = /* ]] || target="$invocation_cwd/$target"
    if python3 - "$vault" "$target" <<'PY'
import os, sys
root, target = map(os.path.realpath, sys.argv[1:])
raise SystemExit(0 if os.path.commonpath([root + "/wiki", target]) == root + "/wiki" else 1)
PY
    then
      jq -n --arg reason "Obsidian configuration is invalid; only the selected config file may be repaired before wiki writes" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
      exit 0
    fi
  fi
  if [ "$guard_enabled" = 1 ] && ! reason=$(guard_path "$raw" 2>&1); then
    jq -n --arg reason "$reason" \
      '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
  else
    if [ -z "$owner" ] || [ -z "$tool_id" ]; then
      if is_selected_wiki_target "$raw"; then
        deny_prewrite "Wiki writer identity is missing; refusing this wiki write because its prewrite snapshot cannot be owned safely"
      fi
      exit 0
    fi
    if ! capture_error=$(python3 "$script_root/scripts/wiki_lifecycle.py" capture --vault "$vault" --cwd "$invocation_cwd" --path "$raw" --owner "$owner" --tool "$tool_id" 2>&1); then
      if is_selected_wiki_target "$raw"; then
        deny_prewrite "Wiki prewrite state could not be captured; refusing this wiki write: $capture_error"
      fi
    fi

  fi
  ;;

start)
  if [ -n "$config_error" ]; then
    printf 'Obsidian integration configuration needs repair: %s\n' "$config_error"
    printf 'Stop wiki writes. Propose the exact minimal change to the selected integration config, preserve unrelated fields, and obtain explicit approval for those changes. Edit only that config; revalidate it in this session before resuming. Do not reset settings or enable autoCommit.\n'
  fi
  printf 'Configured Obsidian wiki: %s/wiki (OKF v0.2; writes are guarded and validated; sync is Obsidian Git-owned).\n' "$vault"
  python3 "$script_root/scripts/wiki_lifecycle.py" diagnose --vault "$vault"
  ;;

prewrite-capture)
  raw=${1:-}
  [ -n "$raw" ] || exit 0
  shift
  if [ -n "$config_error" ]; then exit 0; fi
  if ! guard_path "$raw" >/dev/null; then exit 0; fi
  python3 "$script_root/scripts/wiki_lifecycle.py" capture --vault "$vault" --cwd "$invocation_cwd" --path "$raw" "$@"
  ;;

postwrite)
  if [ -n "$config_error" ]; then
    echo "obsidian lifecycle: $config_error; refusing wiki synchronization until config repair and revalidation" >&2
    exit 1
  fi
  requested=()
  owner=
  tool_id=
  from_hook=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --owner) owner=${2:-}; shift 2 ;;
      --tool) tool_id=${2:-}; shift 2 ;;
      *) requested+=("$1"); shift ;;
    esac
  done
  if [ ${#requested[@]} -eq 0 ] && command -v jq >/dev/null 2>&1; then
    input=$(cat)
    tool_ok=$(printf '%s' "$input" | jq -er '.tool_name | . == "Edit" or . == "MultiEdit" or . == "NotebookEdit" or . == "Write"' 2>/dev/null) || exit 1
    [ "$tool_ok" = true ] || exit 0
    raw=$(printf '%s' "$input" | jq -er '.tool_input.file_path // .tool_input.notebook_path // empty') || exit 1
    owner=$(printf '%s' "$input" | claude_owner)
    tool_id=$(printf '%s' "$input" | jq -r '.tool_use_id // empty')
    from_hook=1
    requested+=("$raw")
  fi
  [ ${#requested[@]} -gt 0 ] || exit 0
  if [ "$from_hook" = 1 ] && { [ -z "$owner" ] || [ -z "$tool_id" ]; }; then
    if is_selected_wiki_target "${requested[0]}"; then
      echo "obsidian lifecycle: postwrite owner/tool identity unavailable; session stop must recover its own capture" >&2
      exit 1
    fi
    exit 0
  fi
  mw=$(middleware_dir)
  for raw in "${requested[@]}"; do
    guard_path "$raw" >/dev/null || exit 1
    record_args=(--vault "$vault" --cwd "$invocation_cwd" --path "$raw" --validator "$mw/validate.py")
    if [ -n "$owner" ] && [ -n "$tool_id" ]; then record_args+=(--owner "$owner" --tool "$tool_id"); fi
    python3 "$script_root/scripts/wiki_lifecycle.py" record "${record_args[@]}" || exit 1
  done
  ;;

finalize|finalize-read)
  owner=
  recover_owner=
  if [ "$cmd" = finalize-read ]; then
    command -v jq >/dev/null 2>&1 || exit 2
    input=$(cat)
    owner=$(printf '%s' "$input" | claude_owner)
    raw=$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_input.path // empty')
    [ -n "$raw" ] || exit 0
    target=$raw
    [[ "$target" = /* ]] || target="$invocation_cwd/$target"
    if ! python3 - "$vault" "$target" <<'PY'
import os, sys
vault, target = map(os.path.realpath, sys.argv[1:])
try:
    selected_wiki = os.path.join(vault, "wiki")
    is_index = os.path.basename(target).lower() == "index.md"
    inside = os.path.commonpath([selected_wiki, target]) == selected_wiki
except ValueError:
    inside = False
raise SystemExit(0 if is_index and inside else 1)
PY
    then exit 0; fi
    if [ -z "$owner" ]; then
      jq -n --arg reason "Reader session identity is unavailable; navigation freshness cannot be verified safely" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
      exit 0
    fi
  else
    while [ $# -gt 0 ]; do
      case "$1" in
        --owner) owner=${2:-}; shift 2 ;;
        --recover-owner) recover_owner=${2:-}; shift 2 ;;
        *) echo "obsidian lifecycle: unknown finalize argument: $1" >&2; exit 2 ;;
      esac
    done
  fi
  if [ -n "$config_error" ]; then
    if [ "$cmd" = finalize-read ]; then
      jq -n --arg reason "Obsidian config is invalid; navigation freshness cannot be verified" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
      exit 0
    fi
    echo "obsidian lifecycle: $config_error; pending wiki work is retained until config repair" >&2
    exit 1
  fi

  mw=$(middleware_dir)
  retrieval=
  feature_enabled retrievalRefresh && retrieval="$script_root/scripts/bm25-index.py"
  finalize_args=(--vault "$vault" --middleware "$mw")
  [ -n "$retrieval" ] && finalize_args+=(--retrieval-script "$retrieval")
  [ -n "$owner" ] && finalize_args+=(--owner "$owner")
  [ -n "$recover_owner" ] && finalize_args+=(--recover-owner "$recover_owner")
  if finalize_output=$(python3 "$script_root/scripts/wiki_lifecycle.py" finalize "${finalize_args[@]}" 2>&1); then
    printf '%s\n' "$finalize_output"
    status=0
  else
    status=$?
    if [ "$cmd" = finalize-read ]; then
      jq -n --arg reason "Pending wiki changes could not be safely finalized; navigation read blocked: $finalize_output" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
      exit 0
    fi
    printf '%s\n' "$finalize_output" >&2
  fi
  exit "$status"
  ;;

stop)
  if [ -n "$config_error" ]; then
    echo "obsidian lifecycle: $config_error; pending wiki work retained until config repair" >&2
    exit 1
  fi
  owner=
  while [ $# -gt 0 ]; do
    case "$1" in
      --owner|--recover-owner) owner=${2:-}; shift 2 ;;
      *) echo "obsidian lifecycle: unknown stop argument: $1" >&2; exit 2 ;;
    esac
  done
  if [ -z "$owner" ] && command -v jq >/dev/null 2>&1; then
    input=$(cat)
    if [ "$(printf '%s' "$input" | jq -r '((.background_tasks // []) | length) > 0 or ((.session_crons // []) | length) > 0')" = true ]; then
      echo "obsidian lifecycle: Claude Stop has background tasks or session crons; owner recovery deferred until they settle" >&2
      exit 1
    fi
    owner=$(printf '%s' "$input" | claude_owner)
  fi
  mw=$(middleware_dir)
  retrieval=
  feature_enabled retrievalRefresh && retrieval="$script_root/scripts/bm25-index.py"
  finalize_args=(--vault "$vault" --middleware "$mw")
  [ -n "$retrieval" ] && finalize_args+=(--retrieval-script "$retrieval")
  if [ -n "$owner" ]; then
    finalize_args+=(--recover-owner "$owner")
  fi
  python3 "$script_root/scripts/wiki_lifecycle.py" finalize "${finalize_args[@]}"
  ;;

*)
  echo "usage: obsidian-session.sh {start|postwrite|finalize|stop} [touched-page ...]" >&2
  exit 2
  ;;
esac
