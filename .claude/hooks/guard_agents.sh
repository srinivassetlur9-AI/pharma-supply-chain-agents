#!/bin/bash
input=$(cat)
cmd=$(jq -r '.tool_input.command // ""' <<<"$input")
agent=$(jq -r '.agent_type // "main"' <<<"$input")

# Nobody approves or changes settings through the shell, and nobody reads tokens or touches the database directly.
if grep -qE '/approve|/reject|/settings/|/orders/manual' <<<"$cmd"; then
  echo "Blocked: approvals, manual orders and settings are done by a logged-in person in the web app." >&2; exit 2
fi
if grep -qE '\.env\.agents|sqlite3|app\.db' <<<"$cmd"; then
  echo "Blocked: agents use scripts/agent_api.py only." >&2; exit 2
fi

if grep -q 'agent_api.py' <<<"$cmd"; then
  case "$agent" in
    dealer-agent)       allowed="summary dealer" ;;
    cf-agent)           allowed="summary cf" ;;
    manufacturer-agent) allowed="summary manufacturer" ;;
    main)               allowed="" ;;
    *)                  echo "Blocked: unknown agent $agent." >&2; exit 2 ;;
  esac
  if [ -n "$allowed" ] && ! grep -q "agent_api.py $allowed" <<<"$cmd"; then
    echo "Blocked: $agent may only run '$allowed' (read-only, its own page)." >&2; exit 2
  fi
  if [ "$agent" = "main" ] && grep -qE 'summary (dealer|cf|manufacturer)' <<<"$cmd"; then
    echo "Blocked: the main agent delegates other roles data to their subagents." >&2; exit 2
  fi
fi
exit 0
