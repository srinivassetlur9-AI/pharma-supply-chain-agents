#!/bin/bash
path=$(jq -r '.tool_input.file_path // ""')
if [[ "$path" == *app.db || "$path" == *.env.agents || "$path" == *.xlsx ]]; then
  echo "Blocked: the database, tokens and source workbook are never edited by an agent." >&2; exit 2
fi
exit 0
