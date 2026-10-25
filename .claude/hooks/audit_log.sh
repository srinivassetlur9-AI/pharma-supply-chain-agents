#!/bin/bash
mkdir -p "${CLAUDE_PROJECT_DIR}/data"
jq -c '{ts: (now | todate), agent: (.agent_type // "main"), tool: .tool_name, input: .tool_input}' \
  >> "${CLAUDE_PROJECT_DIR}/data/agent_audit.log"
exit 0
