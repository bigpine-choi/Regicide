#!/usr/bin/env bash
set -euo pipefail
cd /mnt/c/USER/dev/Regicide-pawn-structure
out=build/neural-improvement-diagnosis/linux/native
g++ -std=c++20 -O2 -pthread -Wall -Wextra -Wpedantic -Werror -I include src/main.cpp src/uci.cpp "$out/board.o" "$out/search.o" "$out/evaluation.o" "$out/transposition.o" "$out/see.o" "$out/neural.o" -o "$out/regicide"
"$out/regicide" --help
