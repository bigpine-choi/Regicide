#pragma once

#include "regicide/board.hpp"

#include <memory>

namespace regicide {

// Material plus original, coordinate-based piece-position bonuses. Positive
// scores favor the side to move; 100 centipawns = one pawn. Middlegame king
// safety/development gradually gives way to endgame king/pawn activity as
// non-pawn material disappears. Terminal results and draw claims belong to search.
int evaluate_position(const Board& board);

// Original 768 -> 16 clipped-ReLU -> 1 network parameters. Both color
// perspectives share weights. Loaded models are immutable and range checked.
// Direct in-memory models require the same limits as loaded files. Format v1:
// input [-127,127], bias [-4096,4096], input_scale 1. Format v2:
// input [-32767,32767], bias [-1048576,1048576], input_scale a power of two
// in [2,256]. Both use output [-8192,8192] and denominator [1,65536].
// V2 accumulates fixed-point input/bias values, clips at 127 * input_scale,
// and divides the final white numerator once by output_scale. The latter
// includes input_scale and must be divisible by it.
struct NeuralModel {
    std::array<std::array<int, 16>, 768> input_weights{};
    std::array<int, 16> hidden_bias{};
    std::array<int, 16> output_weights{};
    int output_scale = 1;
    int format_version = 1;
    int input_scale = 1;
};
const NeuralModel& builtin_neural_model() noexcept;
std::shared_ptr<const NeuralModel> load_neural_model(const std::string& path);
// Save a validated model to a new path; existing files are never replaced.
void save_neural_model(const NeuralModel& model, const std::string& path);
std::uint64_t neural_model_checksum(const NeuralModel& model) noexcept;

// Experimental network; the builtin model is an untrained bootstrap.
// Independent full-refresh reference; side-to-move centipawns, not mate scores.
int evaluate_neural(const Board& board);
int evaluate_neural(const Board& board, const NeuralModel& model) noexcept;

// Search-local raw sums for both perspectives. Never clip stored sums: clipping
// belongs to evaluate(), so removing a feature restores the exact prior value.
// Copy before a move and assign that snapshot after Board::undo(). This class
// owns no board/history and performs no allocation during update or evaluation.
class NeuralAccumulator {
public:
    // The immutable model must outlive this accumulator and its snapshots.
    explicit NeuralAccumulator(const Board& board,
                               const NeuralModel& model = builtin_neural_model()) noexcept;
    // Precondition: before -> after is this freshly generated legal orthodox
    // move, and this accumulator matches before. Examines only changed squares.
    void update_generated(const Position& before, const Position& after, Move move) noexcept;
    int evaluate(Color turn) const noexcept;
    bool operator==(const NeuralAccumulator&) const = default;
private:
    const NeuralModel* model_;
    std::array<std::array<int, 16>, 2> sums_{};
    void add_piece(char piece, int square, int sign) noexcept;
};

} // namespace regicide
