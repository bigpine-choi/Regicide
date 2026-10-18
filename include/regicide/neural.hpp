#pragma once

#include "regicide/board.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <istream>
#include <memory>
#include <string>

namespace regicide {

// Small, immutable experimental evaluator. V1 replaces positional evaluation;
// V2 preserves it and learns a bounded correction. Terminal and draw
// adjudication remain the searcher's responsibility.
class NeuralEvaluator final {
public:
    static constexpr std::size_t input_count = 768;
    static constexpr std::size_t hidden1_count = 32;
    static constexpr std::size_t hidden2_count = 16;
    static constexpr int residual_scale = 600;
    static constexpr int correction_limit = 100;
    static constexpr std::size_t parameter_count =
        input_count * hidden1_count + hidden1_count +
        hidden1_count * hidden2_count + hidden2_count + hidden2_count + 1;
    static constexpr std::size_t max_file_bytes = 4 * 1024 * 1024;

    // Experimental NNUE-style inference quantizes only the first affine
    // layer. Both fixed perspectives are kept: changing the side to move
    // changes both the piece channels and the rank orientation of the input.
    static constexpr std::int64_t nnue_scale = std::int64_t{1} << 24;
    struct Accumulator {
        // Unclipped first-layer sums, indexed by white/black perspective.
        std::array<std::array<std::int64_t, hidden1_count>, 2> hidden{};
        int material_white = 0;
        bool operator==(const Accumulator&) const = default;
    };

    // Strict REGICIDE_NN_V1 / V2 formats; rejects malformed, extra, non-finite,
    // or out-of-range parameters with std::runtime_error. The stream overload
    // has the same size limit and validation as file loading.
    static std::shared_ptr<const NeuralEvaluator> load(const std::string& path);
    static std::shared_ptr<const NeuralEvaluator> load(std::istream& input);

    // Score in centipawns from the side-to-move perspective. Inputs contain
    // only pieces and turn; castling/EP/history are handled by the rules/search.
    int evaluate(const Board& board, bool king_pawn_shield = true,
                 bool pawn_structure = true) const;

    // These states belong to this immutable model. A parent must describe
    // before, and evaluation requires a state describing board. Preserve a
    // parent snapshot for undo; no inverse floating-point updates are needed.
    Accumulator refresh(const Board& board) const;
    Accumulator update(const Accumulator& parent, const Position& before,
                       const Position& after) const;
    int evaluate_nnue(const Board& board, const Accumulator& accumulator,
                      bool king_pawn_shield = true, bool pawn_structure = true) const;
    // Full quantized reference for parity checks, independent of move updates.
    int evaluate_nnue_full(const Board& board, bool king_pawn_shield = true,
                           bool pawn_structure = true) const;

private:
    NeuralEvaluator() = default;
    void accumulate_piece(Accumulator& accumulator, char piece,
                          std::size_t square, int sign) const;
    int evaluate_hidden(const Board& board, std::array<double, hidden1_count>& h1,
                        int material, bool king_pawn_shield, bool pawn_structure) const;
    bool classical_residual_ = false;
    // Runtime layout is [input feature][hidden neuron]. The model file keeps
    // its original [hidden neuron][input feature] order; loading transposes it.
    std::array<double, input_count * hidden1_count> w1_{};
    std::array<double, hidden1_count> b1_{};
    // |parameter| <= 100 and scale 2^24 fit in int32. Even 64 occupied
    // squares plus bias remain far below int64's limit when accumulated.
    std::array<std::int32_t, input_count * hidden1_count> nnue_w1_{};
    std::array<std::int32_t, hidden1_count> nnue_b1_{};
    std::array<double, hidden1_count * hidden2_count> w2_{};
    std::array<double, hidden2_count> b2_{};
    std::array<double, hidden2_count> w3_{};
    double b3_ = 0;
};

} // namespace regicide
