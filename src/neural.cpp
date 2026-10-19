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
void NeuralAccumulator::add_piece(char piece, int square, int sign) noexcept {
    constexpr std::string_view pieces = "PNBRQKpnbrqk";
    const auto index = pieces.find(piece);
    if (index == std::string_view::npos) return;
    const int type = static_cast<int>(index);
    const auto& white = input_weights[type * 64 + square];
    const auto& black = input_weights[((type + 6) % 12) * 64 + (square ^ 56)];
    for (int h = 0; h < hidden; ++h) {
        sums_[0][h] += sign * white[h];
        sums_[1][h] += sign * black[h];
    }
}

NeuralAccumulator::NeuralAccumulator(const Board& board) noexcept {
    for (int square = 0; square < 64; ++square)
        add_piece(board.position().squares[square], square, 1);
}

void NeuralAccumulator::update_generated(const Position& before, const Position& after, Move move) noexcept {
    const auto changed = [&](int square) {
        const char previous = before.squares[square], next = after.squares[square];
        if (previous == next) return;
        add_piece(previous, square, -1);
        add_piece(next, square, 1);
    };
    changed(move.from);
    changed(move.to); // Includes captures and replacing a pawn on promotion.
    const char piece = before.squares[move.from];
    if ((piece == 'P' || piece == 'p') && move.to == before.en_passant)
        changed(move.to + (piece == 'P' ? -8 : 8));
    if ((piece == 'K' || piece == 'k') && (move.to - move.from == 2 || move.from - move.to == 2)) {
        const bool kingside = move.to > move.from;
        changed(move.from + (kingside ? 3 : -4)); // Rook source.
        changed(move.from + (kingside ? 1 : -1)); // Rook destination.
    }
}

int NeuralAccumulator::evaluate(Color turn) const noexcept {
    int white = 0;
    for (int h = 0; h < hidden; ++h)
        white += (std::clamp(sums_[0][h], 0, 127) - std::clamp(sums_[1][h], 0, 127)) * output_weights[h];
    const int score = std::clamp(white, -28000, 28000);
    return turn == Color::white ? score : -score;
}
} // namespace regicide
