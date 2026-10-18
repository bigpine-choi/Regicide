#!/usr/bin/env bash
set -uo pipefail
cd $REPO
logs=build/shield-opt-validation
printf 'start_utc: %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%S.%NZ')" > "$logs/profile-linux-timeline.txt"
build/shield-opt-linux/profile_evaluation --fen-file tests/data/evaluation_profile_2026_10_18.fens --output "$logs/profile-linux.json" > "$logs/profile-linux.stdout.log" 2> "$logs/profile-linux.stderr.log"
result=$?
printf 'end_utc: %s\nexit_code: %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%S.%NZ')" "$result" >> "$logs/profile-linux-timeline.txt"
cat "$logs/profile-linux-timeline.txt"
exit "$result"