#pragma once

#include "regicide/board.hpp"

namespace regicide {

// Material plus original, coordinate-based piece-position bonuses. Positive
// scores favor the side to move; 100 centipawns = one pawn. Middlegame king
// safety/development gradually gives way to endgame king/pawn activity as
// non-pawn material disappears. Terminal results and draw claims belong to search.
int evaluate_position(const Board& board);

// Experimental, untrained 768 -> 16 clipped-ReLU -> 1 network.
// Full refresh only; scores are side-to-move centipawns, not mate scores.
int evaluate_neural(const Board& board);

} // namespace regicide
