#pragma once

#include "regicide/board.hpp"

namespace regicide {

// Material-only static exchange estimate, in centipawns, for a fresh legal
// move. Alternating captures on its destination use the least-valued legal
// attacker, with optional stopping. This local LVA heuristic does not search
// moves on other squares or prove a tactical result, including forced checks.
// Quiet non-promotions return zero. The board and its history are untouched.
int static_exchange_evaluation(const Board& board, Move move);

} // namespace regicide
