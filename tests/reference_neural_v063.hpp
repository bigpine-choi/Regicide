#pragma once

#include "regicide/board.hpp"

#include <array>
#include <memory>
#include <string>
#include <vector>

namespace regicide::reference_v063 {

// Frozen scalar inference from src/neural.cpp at
// 00dec89b08de4e5f573b18b440a93cd82f4b3311 (v0.6.3). Keep this output-major
// representation and operation order independent of production optimizations.
// Loading below is a fixture reader, not a replacement for production validation.
class NeuralEvaluator final {
public:
    explicit NeuralEvaluator(const std::vector<double>& parameters, bool classical_residual);
    static std::shared_ptr<const NeuralEvaluator> load(const std::string& path);
    int evaluate(const Board& board, bool king_pawn_shield = true,
                 bool pawn_structure = true) const;

private:
    bool classical_residual_;
    std::array<double, 768 * 32> w1_{};
    std::array<double, 32> b1_{};
    std::array<double, 32 * 16> w2_{};
    std::array<double, 16> b2_{};
    std::array<double, 16> w3_{};
    double b3_ = 0;
};

} // namespace regicide::reference_v063
