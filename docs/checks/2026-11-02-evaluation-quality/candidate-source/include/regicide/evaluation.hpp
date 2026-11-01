#pragma once

#include "regicide/board.hpp"

namespace regicide {

// Material plus original, coordinate-based piece-position bonuses. Positive
// scores favor the side to move; 100 centipawns = one pawn. Middlegame king
// safety/development gradually gives way to endgame king/pawn activity as
// non-pawn material disappears. The optional king pawn shelter term is applied
// only to middlegame scores and scales with opposing non-pawn material. The
// independent pawn-structure term rewards passed pawns and penalizes doubled
// and isolated pawns, blended with the same game phase. Disabling pawn structure
// preserves v0.5.5's evaluation; disabling both terms preserves v0.5.3's,
// provided the separate experimental piece-activity term is disabled (default).
// Activity adds pawn-filtered pseudo-mobility, opposite-color bishop pairs and
// rook files. It neither generates legal moves nor resolves pinned-piece safety.
// Terminal results and draw claims belong to search.
int evaluate_position(const Board& board, bool king_pawn_shield = true,
                      bool pawn_structure = true, bool piece_activity = false);

} // namespace regicide
