#!/usr/bin/env bash
set -euo pipefail
cd /mnt/c/USER/dev/Regicide-pawn-structure
out=build/learning-v1-validation/linux
logs=build/learning-v1-validation/linux
mkdir -p "$out/native" "$out/sanitized" "$logs"
{
    date -u +'%Y-%m-%dT%H:%M:%SZ'
    uname -a
    g++ --version
    printf 'source_base_commit: db7fc80be97512a7ec9276f1ff53cd853a9de780\n'
    printf 'actual_code_state: v0.6.0 optional residual neural evaluation; native source frozen during validation\n'
    printf 'source_state: neural API and loader added; default classical engine retained; Windows git identities recorded separately\n'
    printf 'cmake: '; command -v cmake || true
    printf 'ninja: '; command -v ninja || true
    printf 'source_include_test_sha256:\n'
    find src include tests -type f \( -name '*.cpp' -o -name '*.hpp' -o -name '*.h' \) -print0 | sort -z | xargs -0 sha256sum
} > "$logs/linux-environment.txt" 2>&1
build_native() {
    set -x
    for unit in board search evaluation transposition see neural; do
        g++ -std=c++20 -O2 -pthread -Wall -Wextra -Wpedantic -Werror -I include -c "src/$unit.cpp" -o "$out/native/$unit.o"
    done
    g++ -std=c++20 -O2 -pthread -Wall -Wextra -Wpedantic -Werror -I include -c tests/reference_evaluation_v054.cpp -o "$out/native/reference_evaluation_v054.o"
    g++ -std=c++20 -O2 -pthread -Wall -Wextra -Wpedantic -Werror -I include -c tests/reference_evaluation_v056.cpp -o "$out/native/reference_evaluation_v056.o"
    for test in rules board_state; do
        g++ -std=c++20 -O2 -pthread -Wall -Wextra -Wpedantic -Werror -I include "tests/${test}_tests.cpp" "$out/native/board.o" -o "$out/native/${test}_tests"
    done
    for test in search evaluation transposition see neural; do
        extra=()
        if [[ "$test" == evaluation ]]; then extra+=("$out/native/reference_evaluation_v054.o" "$out/native/reference_evaluation_v056.o"); fi
        g++ -std=c++20 -O2 -pthread -Wall -Wextra -Wpedantic -Werror -I include "tests/${test}_tests.cpp" "$out/native/board.o" "$out/native/search.o" "$out/native/evaluation.o" "$out/native/transposition.o" "$out/native/see.o" "$out/native/neural.o" "${extra[@]}" -o "$out/native/${test}_tests"
    done
    for test in rules search evaluation board_state transposition see neural; do
        "$out/native/${test}_tests"
    done
    set +x
}
build_sanitized() {
    set -x
    for unit in board search evaluation transposition see neural; do
        g++ -std=c++20 -O1 -g -pthread -Wall -Wextra -Wpedantic -Werror -fsanitize=address,undefined -fno-omit-frame-pointer -I include -c "src/$unit.cpp" -o "$out/sanitized/$unit.o"
    done
    g++ -std=c++20 -O1 -g -pthread -Wall -Wextra -Wpedantic -Werror -fsanitize=address,undefined -fno-omit-frame-pointer -I include -c tests/reference_evaluation_v054.cpp -o "$out/sanitized/reference_evaluation_v054.o"
    g++ -std=c++20 -O1 -g -pthread -Wall -Wextra -Wpedantic -Werror -fsanitize=address,undefined -fno-omit-frame-pointer -I include -c tests/reference_evaluation_v056.cpp -o "$out/sanitized/reference_evaluation_v056.o"
    for test in evaluation search neural; do
        extra=()
        if [[ "$test" == evaluation ]]; then extra+=("$out/sanitized/reference_evaluation_v054.o" "$out/sanitized/reference_evaluation_v056.o"); fi
        g++ -std=c++20 -O1 -g -pthread -Wall -Wextra -Wpedantic -Werror -fsanitize=address,undefined -fno-omit-frame-pointer -I include "tests/${test}_tests.cpp" "$out/sanitized/board.o" "$out/sanitized/search.o" "$out/sanitized/evaluation.o" "$out/sanitized/transposition.o" "$out/sanitized/see.o" "$out/sanitized/neural.o" "${extra[@]}" -o "$out/sanitized/${test}_tests"
        ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 "$out/sanitized/${test}_tests"
    done
    set +x
}
build_native > "$logs/linux-native.log" 2>&1
cat "$logs/linux-native.log"
build_sanitized > "$logs/linux-sanitizers.log" 2>&1
cat "$logs/linux-sanitizers.log"
printf 'PASS Linux native six suites and ASan/UBSan evaluation/search\n'