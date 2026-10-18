#pragma once

#include "regicide/board.hpp"

namespace regicide {

// Material plus original, coordinate-based piece-position bonuses. Positive
// scores favor the side to move; 100 centipawns = one pawn. Middlegame king
// safety/development gradually gives way to endgame king/pawn activity as
// non-pawn material disappears. The optional king pawn shelter term is applied
// only to middlegame scores and scales with opposing non-pawn material. False
// preserves the previous positional evaluation. Terminal results and draw
// claims belong to search.
int evaluate_position(const Board& board, bool king_pawn_shield = true);

} // namespace regicide
