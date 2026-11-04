#pragma once

#include "regicide/board.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace regicide {

enum class MateStatus { proven, no_mate_within_bound, unknown };

struct MateLimits {
    int moves = 1; // Attacker moves; maximum horizon = 2*moves-1 plies.
    std::uint64_t nodes = 1000000; // Zero means unlimited.
    std::chrono::milliseconds movetime{0}; // Zero means unlimited.
    std::optional<std::vector<Move>> root_moves; // Nonempty, legal, duplicate-free.
};

struct MateResult {
    MateStatus status = MateStatus::unknown;
    int mate_plies = 0; // Exact shortest root distance only when proven.
    int completed_moves = 0; // Largest fully decided iterative bound.
    std::uint64_t nodes = 0; // All iterations, including unfinished work.
    std::chrono::milliseconds elapsed{0};
    std::vector<Move> pv; // Verified longest defense; empty unless proven.
    std::optional<Move> best_move; // Legal unproven fallback; null for terminal roots.
    std::string reason;
};

// Dedicated exhaustive bounded AND/OR search of a copy with exact history.
// Defender draw claims refute a mate; the attacker may decline its own claim.
// No evaluation, selective pruning, normal search cache, or external engine.
MateResult solve_mate(const Board&, const MateLimits&, const std::atomic_bool* stop = nullptr);

} // namespace regicide
