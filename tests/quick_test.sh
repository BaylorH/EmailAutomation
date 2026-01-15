#!/bin/bash
# Quick test runner for email automation system
# Usage: ./quick_test.sh [scenario_name]

# Check for API key
if [ -z "$OPENAI_API_KEY" ]; then
    echo "❌ OPENAI_API_KEY not set"
    echo ""
    echo "Please set your OpenAI API key:"
    echo "  export OPENAI_API_KEY='sk-...'"
    echo ""
    exit 1
fi

cd "$(dirname "$0")/.."

if [ -n "$1" ]; then
    echo "Running scenario: $1"
    python tests/standalone_test.py -s "$1"
else
    echo "Running all scenarios..."
    python tests/standalone_test.py -r tests/test_results.json
fi
