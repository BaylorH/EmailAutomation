#!/bin/bash
# pre-tool-use.sh — Block access to Jarvis directories
#
# Executor layer must not touch PM knowledge base

# Read JSON from stdin
INPUT=$(cat)

# Extract file path
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# Blocked paths
BLOCKED_PATHS=(
    "/Users/baylorharrison/Documents/GitHub/jarvis"
    "/Users/baylorharrison/Documents/GitHub/jarvis-pm"
    "/Users/baylorharrison/Documents/GitHub/jarvis-migration"
)

for blocked in "${BLOCKED_PATHS[@]}"; do
    if [[ -n "$FILE_PATH" && "$FILE_PATH" == "$blocked"* ]]; then
        jq -n '{
            hookSpecificOutput: {
                hookEventName: "PreToolUse",
                permissionDecision: "deny",
                permissionDecisionReason: "Executor cannot access Jarvis directories. Complete your task within this repo."
            }
        }'
        exit 0
    fi
done

exit 0
