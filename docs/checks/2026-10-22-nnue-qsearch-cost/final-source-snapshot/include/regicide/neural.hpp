#pragma once

#include "regicide/board.hpp"

#include <array>
#include <cstddef>
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

    // Strict REGICIDE_NN_V1 / V2 formats; rejects malformed, extra, non-finite,
    // or out-of-range parameters with std::runtime_error. The stream overload
    // has the same size limit and validation as file loading.
    static std::shared_ptr<const NeuralEvaluator> load(const std::string& path);
    static std::shared_ptr<const NeuralEvaluator> load(std::istream& input);

    // Score in centipawns from the side-to-move perspective. Inputs contain
    // only pieces and turn; castling/EP/history are handled by the rules/search.
    int evaluate(const Board& board, bool king_pawn_shield = true,
                 bool pawn_structure = true) const;

private:
    NeuralEvaluator() = default;
    bool classical_residual_ = false;
    // Runtime layout is [input feature][hidden neuron]. The model file keeps
    // its original [hidden neuron][input feature] order; loading transposes it.
    std::array<double, input_count * hidden1_count> w1_{};
    std::array<double, hidden1_count> b1_{};
    std::array<double, hidden1_count * hidden2_count> w2_{};
    std::array<double, hidden2_count> b2_{};
    std::array<double, hidden2_count> w3_{};
    double b3_ = 0;
};

} // namespace regicide
