#pragma once

#include "regicide/board.hpp"

namespace regicide {

// Independent frozen oracle from src/evaluation.cpp at Regicide v0.5.4:
// 07321d8b27b95dad9a1fc66c9170ab8085829868. The .cpp differs only in its
// include and public function name. Keep its formulas and control flow intact.
int evaluate_position_v054(const Board& board, bool king_pawn_shield = true);

} // namespace regicide
