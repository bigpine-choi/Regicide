#include "regicide/evaluation.hpp"
#include "regicide/nnue.hpp"
#include <array>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>
int classical_legacy(const regicide::Board& board) { return regicide::evaluate_position(board, false, false); }
int classical_baseline(const regicide::Board& board) { return regicide::evaluate_position(board, true, true, false); }
int classical_activity(const regicide::Board& board) { return regicide::evaluate_position(board, true, true, true); }

// Pure evaluator cost only: no legal-move generation/search in the timed region.
// An optional file supplies one standard FEN per line; blank/# lines are ignored.
// Repeated runs alternate order, retain a checksum, and do not imply strength.
int activity_benchmark(const char* fen_file) {
 std::vector<regicide::Board> boards;
 if (fen_file) {
  std::ifstream input(fen_file);
  if (!input) throw std::runtime_error("cannot open FEN file");
  std::string line;
  while (std::getline(input, line)) {
   if (!line.empty() && line.back() == '\r') line.pop_back();
   if (line.empty() || line.front() == '#') continue;
   boards.emplace_back(line);
  }
  if (input.bad() || boards.empty()) throw std::runtime_error("FEN file is unreadable or empty");
 } else {
  boards.emplace_back();
  boards.emplace_back("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1");
  boards.emplace_back("7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1");
  boards.emplace_back("7k/P7/8/8/8/8/8/6K1 w - - 0 1");
 }
 constexpr int iterations = 1000000, warmup = 1000;
 std::cout << "round,order,mode,boards,evaluations,elapsed_ns,checksum\n";
 for (int round = 0; round < 6; ++round) for (int order = 0; order < 2; ++order) {
  const bool activity = ((round + order) % 2) != 0;
  auto evaluator = activity ? classical_activity : classical_baseline;
  std::int64_t checksum = 0;
  for (int i = 0; i < warmup; ++i) checksum += evaluator(boards[i % boards.size()]);
  const auto start = std::chrono::steady_clock::now();
  for (int i = 0; i < iterations; ++i) checksum += evaluator(boards[i % boards.size()]);
  const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - start).count();
  std::cout << round << ',' << order << ',' << (activity ? "classical_activity" : "classical_baseline") << ','
            << boards.size() << ',' << iterations << ',' << ns << ',' << checksum << '\n';
 }
 return 0;
}

int main(int argc, char** argv) {
 if (argc > 1) {
  if ((argc != 2 && argc != 3) || std::string_view(argv[1]) != "--activity") {
   std::cerr << "usage: evaluation_benchmark [--activity [fen-file]]\n";
   return 1;
  }
  try { return activity_benchmark(argc == 3 ? argv[2] : nullptr); }
  catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
 }
 const std::array<regicide::Board, 4> boards{regicide::Board{},
 regicide::Board("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"),
 regicide::Board("7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1"),
 regicide::Board("7k/P7/8/8/8/8/8/6K1 w - - 0 1")};
 for (int round = 0; round < 6; ++round) for (int order = 0; order < 2; ++order) {
  const bool neural = ((round + order) % 2) != 0;
  auto evaluator = neural ? static_cast<int (*)(const regicide::Board&)>(regicide::evaluate_neural) : classical_legacy;
  std::int64_t checksum = 0;
  for (int i = 0; i < 1000; ++i) checksum += evaluator(boards[i % boards.size()]);
  const auto start = std::chrono::steady_clock::now();
  for (int i = 0; i < 1000000; ++i) checksum += evaluator(boards[i % boards.size()]);
  const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - start).count();
  std::cout << round << ',' << (neural ? "neural" : "classical") << ',' << 1000000 << ',' << ns << ',' << checksum << '\n';
 }
}
