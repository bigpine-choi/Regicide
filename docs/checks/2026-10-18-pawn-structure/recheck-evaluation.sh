#!/usr/bin/env bash
set -euo pipefail
cd $WSL_REPO
out=build/pawn-structure-linux
{
    date -u +'%Y-%m-%dT%H:%M:%SZ'
    printf 'Incremental recheck after deterministic en passant/promotion/undo tests; production and all other test source unchanged.\n'
    find src include tests -type f \( -name '*.cpp' -o -name '*.hpp' -o -name '*.h' \) -print0 | sort -z | xargs -0 sha256sum
} > "$out/linux-final-source-sha256.txt" 2>&1
{
    set -x
    g++ -std=c++20 -O2 -pthread -Wall -Wextra -Wpedantic -Werror -I include tests/evaluation_tests.cpp "$out/native/board.o" "$out/native/search.o" "$out/native/evaluation.o" "$out/native/transposition.o" "$out/native/see.o" "$out/native/reference_evaluation_v054.o" -o "$out/native/evaluation_tests"
    "$out/native/evaluation_tests"
} > "$out/linux-evaluation-final.log" 2>&1
{
    set -x
    g++ -std=c++20 -O1 -g -pthread -Wall -Wextra -Wpedantic -Werror -fsanitize=address,undefined -fno-omit-frame-pointer -I include tests/evaluation_tests.cpp "$out/sanitized/board.o" "$out/sanitized/search.o" "$out/sanitized/evaluation.o" "$out/sanitized/transposition.o" "$out/sanitized/see.o" "$out/sanitized/reference_evaluation_v054.o" -o "$out/sanitized/evaluation_tests"
    ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 "$out/sanitized/evaluation_tests"
} > "$out/linux-evaluation-sanitizers-final.log" 2>&1
cat "$out/linux-evaluation-final.log"
cat "$out/linux-evaluation-sanitizers-final.log"
printf 'PASS Linux final evaluation native and ASan/UBSan special-move recheck\n'