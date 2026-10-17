#pragma once

#include "regicide/board.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <optional>
#include <vector>

namespace regicide {

inline constexpr int mate_score = 30000;
inline constexpr int max_search_depth = 64;

// Positive scores favor the side to move; one pawn is 100 centipawns.
int evaluate_material(const Board& board);

struct SearchLimits {
    int depth = 4;
    std::uint64_t nodes = 0; // Zero means unlimited.
    std::chrono::milliseconds movetime{0}; // Zero means unlimited.
};

struct SearchResult {
    std::optional<Move> best_move;
    int score = 0;
    int depth = 0; // Last completely searched iteration; zero is a legal fallback.
    std::uint64_t nodes = 0; // Includes unfinished work and iterative roots.
    std::chrono::milliseconds elapsed{0};
    bool stopped = false;
    std::vector<Move> pv;
};

using SearchCallback = std::function<void(const SearchResult&)>;

// Searches a copy, including repetition history. Terminal roots have no move.
// Optional current/intended draw claims are available at value zero; callers
// must actually claim the draw when choosing that option (UCI cannot claim).
// Invalid depth (outside 1..64) or negative movetime throws invalid_argument.
SearchResult search(const Board& board, const SearchLimits& limits,
                    const std::atomic_bool* stop = nullptr,
                    SearchCallback on_iteration = {});

} // namespace regicide
