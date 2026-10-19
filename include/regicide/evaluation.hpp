#pragma once

#include "regicide/board.hpp"

namespace regicide {

// Material plus original, coordinate-based piece-position bonuses. Positive
// scores favor the side to move; 100 centipawns = one pawn. Middlegame king
// safety/development gradually gives way to endgame king/pawn activity as
// non-pawn material disappears. Terminal results and draw claims belong to search.
int evaluate_position(const Board& board);

// Experimental, untrained 768 -> 16 clipped-ReLU -> 1 network.
// Independent full-refresh reference; side-to-move centipawns, not mate scores.
int evaluate_neural(const Board& board);

// Search-local raw sums for both perspectives. Never clip stored sums: clipping
// belongs to evaluate(), so removing a feature restores the exact prior value.
// Copy before a move and assign that snapshot after Board::undo(). This class
// owns no board/history and performs no allocation during update or evaluation.
class NeuralAccumulator {
public:
    explicit NeuralAccumulator(const Board& board) noexcept;
    // Precondition: before -> after is this freshly generated legal orthodox
    // move, and this accumulator matches before. Examines only changed squares.
    void update_generated(const Position& before, const Position& after, Move move) noexcept;
    int evaluate(Color turn) const noexcept;
    bool operator==(const NeuralAccumulator&) const = default;
private:
    std::array<std::array<int, 16>, 2> sums_{};
    void add_piece(char piece, int square, int sign) noexcept;
};

} // namespace regicide
