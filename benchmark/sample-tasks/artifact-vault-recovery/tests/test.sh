#!/usr/bin/env bash
set -uo pipefail

mkdir -p /logs/verifier

if ! python3 /tests/test_outputs.py; then
    echo '{"coldstart_pass":0,"functional":0,"durability":0,"state_safety":0,"evidence":0,"integrity":0}' > /logs/verifier/reward.json
    echo '{"fatal":"verifier process failed before producing details"}' > /logs/verifier/details.json
fi

cat /logs/verifier/reward.json
