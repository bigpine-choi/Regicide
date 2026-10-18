#!/usr/bin/env bash
set -euo pipefail
cd "${REGICIDE_ROOT:?Set REGICIDE_ROOT to checkout directory}"
out=build/pawn-shield-linux
logs=build/pawn-shield-validation
mkdir -p "$out/native" "$out/sanitized" "$logs"
{
    date -u +'%Y-%m-%dT%H:%M:%SZ'
    uname -a
    g++ --version
    printf 'source_commit: '
    printf '%s (verified by Windows git; Windows worktree links are not understood by WSL git)\n' "$1"
    printf 'cmake: '
    command -v cmake || true
    printf 'ninja: '
    command -v ninja || true
    printf 'core_source_sha256:\n'
    sha256sum src/board.cpp src/search.cpp src/evaluation.cpp src/transposition.cpp src/see.cpp
} > "$logs/linux-environment.txt" 2>&1

build_native() {
    set -x
    for unit in board search evaluation transposition see; do
        g++ -std=c++20 -O2 -pthread -Wall -Wextra -Wpedantic -Werror -I include -c "src/$unit.cpp" -o "$out/native/$unit.o"
    done
    for test in rules board_state; do
        g++ -std=c++20 -O2 -pthread -Wall -Wextra -Wpedantic -Werror -I include "tests/${test}_tests.cpp" "$out/native/board.o" -o "$out/native/${test}_tests"
    done
    for test in search evaluation transposition see; do
        g++ -std=c++20 -O2 -pthread -Wall -Wextra -Wpedantic -Werror -I include "tests/${test}_tests.cpp" "$out/native/board.o" "$out/native/search.o" "$out/native/evaluation.o" "$out/native/transposition.o" "$out/native/see.o" -o "$out/native/${test}_tests"
    done
    for test in rules search evaluation board_state transposition see; do
        "$out/native/${test}_tests"
    done
    set +x
}
build_sanitized() {
    set -x
    for unit in board search evaluation transposition see; do
        g++ -std=c++20 -O1 -g -pthread -Wall -Wextra -Wpedantic -Werror -fsanitize=address,undefined -fno-omit-frame-pointer -I include -c "src/$unit.cpp" -o "$out/sanitized/$unit.o"
    done
    for test in evaluation search; do
        g++ -std=c++20 -O1 -g -pthread -Wall -Wextra -Wpedantic -Werror -fsanitize=address,undefined -fno-omit-frame-pointer -I include "tests/${test}_tests.cpp" "$out/sanitized/board.o" "$out/sanitized/search.o" "$out/sanitized/evaluation.o" "$out/sanitized/transposition.o" "$out/sanitized/see.o" -o "$out/sanitized/${test}_tests"
        ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 "$out/sanitized/${test}_tests"
    done
    set +x
}
build_native > "$logs/linux-native.log" 2>&1
cat "$logs/linux-native.log"
build_sanitized > "$logs/linux-sanitizers.log" 2>&1
cat "$logs/linux-sanitizers.log"
printf 'PASS Linux native six suites and ASan/UBSan evaluation/search\n'
