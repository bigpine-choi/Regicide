#include "regicide/evaluation.hpp"

#include <algorithm>
#include <array>
#include <cstdint>
#include <string_view>

namespace regicide {
namespace {
constexpr int hidden = 16;
// Original deterministic bootstrap weights, model bootstrap-v1. No training data
// or external weights. First six channels count own pieces; others exercise
// square-dependent nonlinear features. Both perspectives share parameters.
constexpr auto weights() {
    std::array<std::array<int, hidden>, 768> result{};
    std::uint32_t state = 0x52454731U;
    for (int feature = 0; feature < 768; ++feature) {
        const int piece = feature / 64;
        if (piece < 6) result[feature][piece] = 1;
        for (int h = 6; h < hidden; ++h) {
            state = state * 1664525U + 1013904223U;
            result[feature][h] = static_cast<int>((state >> 16) % 7) - 3;
        }
    }
    return result;
}
constexpr auto input_weights = weights();
constexpr std::array<int, hidden> output_weights{
    100, 320, 330, 500, 900, 0, 1, -1, 1, -1, 1, -1, 1, -1, 1, -1};
int perspective(const Position& position, Color viewer) {
    std::array<int, hidden> accumulator{};
    constexpr std::string_view pieces = "PNBRQKpnbrqk";
    for (int square = 0; square < 64; ++square) {
        const auto index = pieces.find(position.squares[square]);
        if (index == std::string_view::npos) continue;
        const int piece = static_cast<int>(index);
        const int relative_piece = viewer == Color::white ? piece : (piece + 6) % 12;
        const int relative_square = viewer == Color::white ? square : square ^ 56;
        const auto& row = input_weights[relative_piece * 64 + relative_square];
        for (int h = 0; h < hidden; ++h) accumulator[h] += row[h];
    }
    int score = 0;
    for (int h = 0; h < hidden; ++h)
        score += std::clamp(accumulator[h], 0, 127) * output_weights[h];
    return score;
}
} // namespace
int evaluate_neural(const Board& board) {
    const auto& position = board.position();
    const int white = perspective(position, Color::white) - perspective(position, Color::black);
    // Keep arbitrary promoted-material positions outside search's mate band.
    const int score = std::clamp(white, -28000, 28000);
    return position.turn == Color::white ? score : -score;
}
} // namespace regicide
