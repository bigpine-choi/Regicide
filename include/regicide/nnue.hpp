#pragma once

#include "regicide/board.hpp"

#include <array>
#include <cstdint>
#include <memory>
#include <string>

namespace regicide {

// 768 piece-square inputs -> 16 clipped-ReLU -> 1 network parameters. Both
// color perspectives share weights. V3 adds one relative side-to-move input,
// active only for the perspective whose color has the move. Loaded models are
// immutable and range checked.
// Direct in-memory models require the same limits as loaded files. Format v1:
// input [-127,127], bias [-4096,4096], input_scale 1. Format v2:
// input [-32767,32767], bias [-1048576,1048576], input_scale a power of two
// in [2,256]. V3 uses the same limits as v2, including its turn input weights.
// All formats use output [-8192,8192] and denominator [1,65536].
// V2/v3 accumulate fixed-point input/bias values, clip at 127 * input_scale,
// and divides the final white numerator once by output_scale. The latter
// includes input_scale and must be divisible by it.
struct NeuralModel {
    std::array<std::array<int, 16>, 768> input_weights{};
    std::array<int, 16> hidden_bias{};
    std::array<int, 16> output_weights{};
    int output_scale = 1;
    int format_version = 1;
    int input_scale = 1;
    // Kept separate so v1/v2 retain exactly 768 serialized rows/checksums.
    // These weights must be zero for a legacy model.
    std::array<int, 16> turn_weights{};
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
    // turn is the actual side to move. In v3 it activates a feature, so
    // evaluate(opposite(turn)) need not equal -evaluate(turn).
    int evaluate(Color turn) const noexcept;
    bool operator==(const NeuralAccumulator&) const = default;
private:
    const NeuralModel* model_;
    std::array<std::array<int, 16>, 2> sums_{};
    void add_piece(char piece, int square, int sign) noexcept;
};

} // namespace regicide
