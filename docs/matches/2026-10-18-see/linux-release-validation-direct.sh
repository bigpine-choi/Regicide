set -euo pipefail
cd /mnt/c/Users/hanso/.codex/.chatgpt-projects/g-p-6a8b058617088191ac7cb44895558f31/Regicide
mkdir -p build-see-linux-release
{
  date --iso-8601=seconds
  uname -a
  g++ --version
  for part in board search evaluation transposition see; do
    g++ -std=c++20 -O3 -DNDEBUG -Wall -Wextra -Wpedantic -Werror -pthread -Iinclude -c "src/$part.cpp" -o "build-see-linux-release/$part.o"
  done
  for test in rules search evaluation board_state transposition see; do
    g++ -std=c++20 -O3 -DNDEBUG -Wall -Wextra -Wpedantic -Werror -pthread -Iinclude "tests/${test}_tests.cpp" build-see-linux-release/{board,search,evaluation,transposition,see}.o -o "build-see-linux-release/regicide_${test}_tests"
    echo "=== $test ==="
    "build-see-linux-release/regicide_${test}_tests"
  done
} 2>&1 | tee build/see-evidence/linux-release-validation-direct.log