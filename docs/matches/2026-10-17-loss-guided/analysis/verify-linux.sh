#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$repo_dir"
mkdir -p build-strength-linux/release build-strength-linux/debug
g++ --version > build-strength-linux/compiler.txt
release_flags=(-std=c++20 -O2 -DNDEBUG -Wall -Wextra -Wpedantic -Werror -Iinclude -pthread)
debug_flags=(-std=c++20 -O0 -g -fsanitize=address,undefined -fno-omit-frame-pointer -Wall -Wextra -Wpedantic -Werror -Iinclude -pthread)
common_sources=(board search evaluation transposition)
release_objects=()
for source in "${common_sources[@]}" uci; do
    object="build-strength-linux/release/$source.o"
    g++ "${release_flags[@]}" -c "src/$source.cpp" -o "$object"
    release_objects+=("$object")
done
g++ "${release_flags[@]}" src/main.cpp "${release_objects[@]}" -o build-strength-linux/release/regicide
for target in rules search evaluation board_state transposition; do
    binary="build-strength-linux/release/${target}_tests"
    g++ "${release_flags[@]}" "tests/${target}_tests.cpp" "${release_objects[@]}" -o "$binary"
    "$binary" > "build-strength-linux/release/${target}.log" 2>&1
    tail -n 1 "build-strength-linux/release/${target}.log"
done
./build-strength-linux/release/regicide --help > build-strength-linux/release/engine-help.log
./build-strength-linux/release/regicide search 3 --hash 1 > build-strength-linux/release/engine-search.log
debug_objects=()
for source in "${common_sources[@]}"; do
    object="build-strength-linux/debug/$source.o"
    g++ "${debug_flags[@]}" -c "src/$source.cpp" -o "$object"
    debug_objects+=("$object")
done
for target in transposition; do
    binary="build-strength-linux/debug/${target}_sanitized"
    g++ "${debug_flags[@]}" "tests/${target}_tests.cpp" "${debug_objects[@]}" -o "$binary"
    ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 "$binary" > "build-strength-linux/debug/${target}.log" 2>&1
    tail -n 1 "build-strength-linux/debug/${target}.log"
done
echo 'PASS Linux strict Release whole engine + five test suites; Debug AddressSanitizer/UndefinedBehaviorSanitizer search + transposition.'
