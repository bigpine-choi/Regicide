#include "regicide/see.hpp"
#include <chrono>
#include <cstdint>
namespace regicide {
int reference_static_exchange_evaluation(const Board&, Move);
std::uint64_t measured_see_calls = 0;
std::int64_t measured_see_nanoseconds = 0;
int static_exchange_evaluation(const Board& board, Move move) {
    const auto start = std::chrono::steady_clock::now();
    const int result = reference_static_exchange_evaluation(board, move);
    measured_see_nanoseconds += std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - start).count();
    ++measured_see_calls;
    return result;
}
}
