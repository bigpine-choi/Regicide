set -euo pipefail
cd /mnt/c/Users/hanso/.codex/.chatgpt-projects/g-p-6a8b058617088191ac7cb44895558f31/Regicide
mkdir -p build-see-linux-sanitized
{
  date --iso-8601=seconds
  g++ --version
  for part in board search evaluation transposition see; do
    g++ -std=c++20 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer -fno-pie -Wall -Wextra -Wpedantic -Werror -pthread -Iinclude -c "src/$part.cpp" -o "build-see-linux-sanitized/$part.o"
  done
  for test in search see; do
    g++ -std=c++20 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer -fno-pie -no-pie -Wall -Wextra -Wpedantic -Werror -pthread -Iinclude "tests/${test}_tests.cpp" build-see-linux-sanitized/{board,search,evaluation,transposition,see}.o -o "build-see-linux-sanitized/regicide_${test}_tests"
    echo "=== ASan/UBSan $test ==="
    ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 UBSAN_OPTIONS=print_stacktrace=1:halt_on_error=1 "build-see-linux-sanitized/regicide_${test}_tests"
  done
} 2>&1 | tee build/see-evidence/linux-sanitized-validation.log