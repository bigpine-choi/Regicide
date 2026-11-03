#include "regicide/evaluation.hpp"
#include <array>
#include <chrono>
#include <cstdint>
#include <iostream>
int main() {
 const std::array<regicide::Board, 4> boards{regicide::Board{},
 regicide::Board("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"),
 regicide::Board("7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1"),
 regicide::Board("7k/P7/8/8/8/8/8/6K1 w - - 0 1")};
 for (int round = 0; round < 6; ++round) for (int order = 0; order < 2; ++order) {
  const bool neural = ((round + order) % 2) != 0;
  auto evaluator = neural ? static_cast<int (*)(const regicide::Board&)>(regicide::evaluate_neural) : regicide::evaluate_position;
  std::int64_t checksum = 0;
  for (int i = 0; i < 1000; ++i) checksum += evaluator(boards[i % boards.size()]);
  const auto start = std::chrono::steady_clock::now();
  for (int i = 0; i < 1000000; ++i) checksum += evaluator(boards[i % boards.size()]);
  const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - start).count();
  std::cout << round << ',' << (neural ? "neural" : "classical") << ',' << 1000000 << ',' << ns << ',' << checksum << '\n';
 }
}
