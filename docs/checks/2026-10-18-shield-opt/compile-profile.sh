#!/usr/bin/env bash
set -euo pipefail
cd $REPO
out=build/shield-opt-linux
logs=build/shield-opt-validation
mkdir -p "$out/profile" "$logs"
{
    date -u +'%Y-%m-%dT%H:%M:%S.%NZ'
    uname -a
    g++ --version
    printf 'source_base_commit: 07321d8b27b95dad9a1fc66c9170ab8085829868\n'
    printf 'source_state: uncommitted shield optimization; source hashes below\n'
    printf 'profile_command: build/shield-opt-linux/profile_evaluation --fen-file tests/data/evaluation_profile_2026_10_18.fens --output build/shield-opt-validation/profile-linux.json\n'
    printf 'source_sha256:\n'
    sha256sum scripts/profile_evaluation.cpp tests/reference_evaluation_v054.cpp tests/reference_evaluation_v054.hpp src/evaluation.cpp src/board.cpp include/regicide/evaluation.hpp include/regicide/board.hpp tests/data/evaluation_profile_2026_10_18.fens
} > "$logs/linux-profile-environment.txt" 2>&1
{
    set -x
    for unit in src/board.cpp src/evaluation.cpp tests/reference_evaluation_v054.cpp scripts/profile_evaluation.cpp; do
        name="$(basename "$unit" .cpp)"
        g++ -std=c++20 -O2 -DNDEBUG -Wall -Wextra -Wpedantic -Werror -fno-lto -Iinclude -Itests -c "$unit" -o "$out/profile/$name.o"
    done
    g++ -fno-lto "$out/profile/profile_evaluation.o" "$out/profile/reference_evaluation_v054.o" "$out/profile/evaluation.o" "$out/profile/board.o" -o "$out/profile_evaluation"
    sha256sum "$out/profile_evaluation"
    set +x
} > "$logs/linux-profile-compile.log" 2>&1
cat "$logs/linux-profile-compile.log"