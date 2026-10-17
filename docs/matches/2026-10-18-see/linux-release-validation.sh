set -euo pipefail
cd /mnt/c/Users/hanso/.codex/.chatgpt-projects/g-p-6a8b058617088191ac7cb44895558f31/Regicide
{
  date --iso-8601=seconds
  uname -a
  g++ --version
  cmake --version
  cmake -S . -B build-see-linux-release -DCMAKE_BUILD_TYPE=Release
  cmake --build build-see-linux-release -j2
  ctest --test-dir build-see-linux-release --output-on-failure -V
} 2>&1 | tee build/see-evidence/linux-release-validation.log