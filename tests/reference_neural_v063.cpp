#include "reference_neural_v063.hpp"
#include "regicide/evaluation.hpp"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string_view>

namespace regicide::reference_v063 {
namespace {
constexpr std::size_t input_count = 768;
constexpr std::size_t hidden1_count = 32;
constexpr std::size_t hidden2_count = 16;
constexpr double residual_scale = 600;
constexpr double correction_limit = 100;
constexpr std::size_t parameter_count = 25153;

int piece_type(char piece) {
    switch (piece) {
    case 'P': case 'p': return 0;
    case 'N': case 'n': return 1;
    case 'B': case 'b': return 2;
    case 'R': case 'r': return 3;
    case 'Q': case 'q': return 4;
    case 'K': case 'k': return 5;
    default: return -1;
    }
}
constexpr std::array<int, 6> material_values{100, 320, 330, 500, 900, 0};
} // namespace

NeuralEvaluator::NeuralEvaluator(const std::vector<double>& parameters, bool classical_residual)
    : classical_residual_(classical_residual) {
    if (parameters.size() != parameter_count)
        throw std::runtime_error("Incorrect frozen-reference parameter count.");
    auto source = parameters.begin();
    const auto transfer = [&source](auto& target) {
        std::copy_n(source, target.size(), target.begin());
        source += static_cast<std::ptrdiff_t>(target.size());
    };
    transfer(w1_);
    transfer(b1_);
    transfer(w2_);
    transfer(b2_);
    transfer(w3_);
    b3_ = *source;
}

std::shared_ptr<const NeuralEvaluator> NeuralEvaluator::load(const std::string& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("Cannot open frozen-reference fixture: " + path);
    std::string line;
    if (!std::getline(input, line)) throw std::runtime_error("Missing fixture header.");
    if (!line.empty() && line.back() == '\r') line.pop_back();
    const bool classical = line == "REGICIDE_NN_V2 768 32 16 600 100";
    if (!classical && line != "REGICIDE_NN_V1 768 32 16 600")
        throw std::runtime_error("Unexpected frozen-reference fixture header.");
    std::vector<double> parameters;
    parameters.reserve(parameter_count);
    std::string token;
    while (input >> token) {
        std::string_view number(token);
        if (!number.empty() && number.front() == '+') number.remove_prefix(1);
        double value = 0;
        const auto parsed = std::from_chars(number.data(), number.data() + number.size(),
                                            value, std::chars_format::general);
        if (number.empty() || parsed.ec != std::errc{} ||
            parsed.ptr != number.data() + number.size() || !std::isfinite(value) ||
            std::abs(value) > 100 || parameters.size() == parameter_count)
            throw std::runtime_error("Invalid frozen-reference fixture parameter.");
        parameters.push_back(value);
    }
    if (input.bad()) throw std::runtime_error("Failed reading frozen-reference fixture.");
    return std::make_shared<const NeuralEvaluator>(parameters, classical);
}

// The computation below is copied from the baseline verbatim, apart from its
// namespace and the comments documenting the old implementation. Do not update
// it together with the optimized evaluator: it is the independent old behavior.
int NeuralEvaluator::evaluate(const Board& board, bool king_pawn_shield, bool pawn_structure) const {
    const auto& position = board.position();
    const bool white_turn = position.turn == Color::white;
    std::array<std::size_t, 64> active{};
    std::size_t active_count = 0;
    int material = 0;
    for (std::size_t square = 0; square < position.squares.size(); ++square) {
        const char piece = position.squares[square];
        const int type = piece_type(piece);
        if (type < 0) continue;
        const bool own = (piece >= 'A' && piece <= 'Z') == white_turn;
        material += (own ? 1 : -1) * material_values[static_cast<std::size_t>(type)];
        const std::size_t relative_square = white_turn ? square : (square ^ 56);
        const auto channel = static_cast<std::size_t>(type + (own ? 0 : 6));
        active[active_count++] = channel * 64 + relative_square;
    }

    std::array<double, hidden1_count> h1{};
    for (std::size_t row = 0; row < hidden1_count; ++row) {
        double value = b1_[row];
        for (std::size_t index = 0; index < active_count; ++index)
            value += w1_[row * input_count + active[index]];
        h1[row] = std::max(0.0, value);
    }
    std::array<double, hidden2_count> h2{};
    for (std::size_t row = 0; row < hidden2_count; ++row) {
        double value = b2_[row];
        for (std::size_t column = 0; column < hidden1_count; ++column)
            value += w2_[row * hidden1_count + column] * h1[column];
        h2[row] = std::max(0.0, value);
    }
    double residual = b3_;
    for (std::size_t column = 0; column < hidden2_count; ++column)
        residual += w3_[column] * h2[column];
    const double unbounded_score = classical_residual_ ?
        evaluate_position(board, king_pawn_shield, pawn_structure) + correction_limit * std::tanh(residual) :
        material + residual_scale * residual;
    const double score = classical_residual_ ? unbounded_score :
                         std::clamp(unbounded_score, -10000.0, 10000.0);
    return static_cast<int>(std::lround(score));
}
} // namespace regicide::reference_v063
