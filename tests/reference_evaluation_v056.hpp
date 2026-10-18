#pragma once

#include "regicide/board.hpp"

namespace regicide {

// Independent frozen oracle from src/evaluation.cpp at Regicide v0.5.6:
// 132f45451d0af68168cfe15507a3fdc069c86d9c (also unchanged at 09090d0).
// The .cpp differs only in its include and public function name. Keep its
// formulas, lookup tables, and control flow intact.
int evaluate_position_v056(const Board& board, bool king_pawn_shield = true,
                           bool pawn_structure = true);

} // namespace regicide
