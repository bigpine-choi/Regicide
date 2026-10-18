#include "regicide/neural.hpp"
#include "regicide/evaluation.hpp"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string_view>

namespace regicide {
namespace {
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

double read_parameter(std::istream& input) {
    std::string token;
    if (!(input >> token))
        throw std::runtime_error("Missing neural model parameter.");
    // from_chars is locale-independent and, unlike MSVC's stream extraction,
    // cannot silently accept hexadecimal float syntax. Require a whole token.
    std::string_view number(token);
    if (!number.empty() && number.front() == '+') {
        number.remove_prefix(1);
        if (!number.empty() && (number.front() == '+' || number.front() == '-'))
            throw std::runtime_error("Invalid neural model numeric sign.");
    }
    double value = 0;
    const auto parsed = std::from_chars(number.data(), number.data() + number.size(),
                                        value, std::chars_format::general);
    if (number.empty() || parsed.ec != std::errc{} || parsed.ptr != number.data() + number.size() ||
        !std::isfinite(value) || std::abs(value) > 100)
        throw std::runtime_error("Invalid neural model parameter (finite magnitude <= 100 required).");
    return value;
}

template <std::size_t N>
void read_parameters(std::istream& input, std::array<double, N>& values) {
    for (double& value : values) value = read_parameter(input);
}
} // namespace

std::shared_ptr<const NeuralEvaluator> NeuralEvaluator::load(const std::string& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("Cannot open neural model file: " + path);
    return load(input);
}

std::shared_ptr<const NeuralEvaluator> NeuralEvaluator::load(std::istream& input) {
    // Bound the input before parsing so an untrusted or accidental large file
    // cannot allocate arbitrary memory. Binary files and trailing junk fail the
    // same strict numeric parser used for every parameter.
    std::string text;
    text.reserve(parameter_count * 4);
    char ch = 0;
    while (input.get(ch)) {
        if (text.size() == max_file_bytes)
            throw std::runtime_error("Neural model exceeds the 4 MiB file limit.");
        text.push_back(ch);
    }
    if (input.bad()) throw std::runtime_error("Failed reading neural model.");
    if (text.size() >= max_file_bytes)
        throw std::runtime_error("Neural model must be smaller than 4 MiB.");
    std::istringstream data(text);
    std::string header_line;
    if (!std::getline(data, header_line))
        throw std::runtime_error("Missing neural model header.");
    std::istringstream header(header_line);
    std::string magic;
    if (!(header >> magic) || (magic != "REGICIDE_NN_V1" && magic != "REGICIDE_NN_V2"))
        throw std::runtime_error("Unsupported neural model version.");
    const bool classical_residual = magic == "REGICIDE_NN_V2";
    std::array<std::string, 4> expected{"768", "32", "16", "600"};
    for (const auto& token : expected) {
        std::string actual;
        if (!(header >> actual) || actual != token)
            throw std::runtime_error("Unsupported neural model header or dimensions.");
    }
    if (classical_residual) {
        std::string limit;
        if (!(header >> limit) || limit != "100")
            throw std::runtime_error("Unsupported neural correction limit.");
    }
    std::string extra;
    if (header >> extra) throw std::runtime_error("Extra data in neural model header.");

    auto model = std::shared_ptr<NeuralEvaluator>(new NeuralEvaluator);
    model->classical_residual_ = classical_residual;
    // Keep the file format unchanged, but make the weights for one occupied
    // feature contiguous. Loading does this once; evaluation uses that layout
    // at every searched position.
    for (std::size_t row = 0; row < hidden1_count; ++row)
        for (std::size_t column = 0; column < input_count; ++column)
            model->w1_[column * hidden1_count + row] = read_parameter(data);
    read_parameters(data, model->b1_);
    read_parameters(data, model->w2_);
    read_parameters(data, model->b2_);
    read_parameters(data, model->w3_);
    model->b3_ = read_parameter(data);
    if (data >> extra) throw std::runtime_error("Extra data after neural model parameters.");
    if (data.bad()) throw std::runtime_error("Failed parsing neural model parameters.");

    // Keep the original doubles and file formats intact. The optional path
    // derives fixed-point first-layer parameters once; later layers retain
    // their original arithmetic. Integer accumulation is order-independent.
    for (std::size_t index = 0; index < model->w1_.size(); ++index)
        model->nnue_w1_[index] = static_cast<std::int32_t>(
            std::llround(model->w1_[index] * nnue_scale));
    for (std::size_t row = 0; row < hidden1_count; ++row)
        model->nnue_b1_[row] = static_cast<std::int32_t>(
            std::llround(model->b1_[row] * nnue_scale));

    return model;
}

int NeuralEvaluator::evaluate(const Board& board, bool king_pawn_shield, bool pawn_structure) const {
    const auto& position = board.position();
    const bool white_turn = position.turn == Color::white;
    std::array<double, hidden1_count> h1 = b1_;
    int material = 0;
    for (std::size_t square = 0; square < position.squares.size(); ++square) {
        const char piece = position.squares[square];
        const int type = piece_type(piece);
        if (type < 0) continue;
        const bool own = (piece >= 'A' && piece <= 'Z') == white_turn;
        if (!classical_residual_)
            material += (own ? 1 : -1) * material_values[static_cast<std::size_t>(type)];
        const std::size_t relative_square = white_turn ? square : (square ^ 56);
        const auto channel = static_cast<std::size_t>(type + (own ? 0 : 6));
        const auto feature = channel * 64 + relative_square;
        const double* weights = w1_.data() + feature * hidden1_count;
        // Each neuron's additions still follow ascending board-square order,
        // exactly as before. Only independent neurons run side by side, which
        // permits contiguous loads and SIMD without reassociating any sum.
        for (std::size_t row = 0; row < hidden1_count; ++row)
            h1[row] += weights[row];
    }

    return evaluate_hidden(board, h1, material, king_pawn_shield, pawn_structure);
}

void NeuralEvaluator::accumulate_piece(Accumulator& accumulator, char piece,
                                      std::size_t square, int sign) const {
    const int type = piece_type(piece);
    if (type < 0) return;
    const bool white_piece = piece >= 'A' && piece <= 'Z';
    accumulator.material_white += sign * (white_piece ? 1 : -1) *
                                  material_values[static_cast<std::size_t>(type)];
    for (std::size_t perspective = 0; perspective < 2; ++perspective) {
        const bool white_perspective = perspective == 0;
        const bool own = white_piece == white_perspective;
        const std::size_t relative_square = white_perspective ? square : (square ^ 56);
        const auto channel = static_cast<std::size_t>(type + (own ? 0 : 6));
        const auto feature = channel * 64 + relative_square;
        const auto* weights = nnue_w1_.data() + feature * hidden1_count;
        for (std::size_t row = 0; row < hidden1_count; ++row)
            accumulator.hidden[perspective][row] +=
                static_cast<std::int64_t>(weights[row]) * sign;
    }
}

NeuralEvaluator::Accumulator NeuralEvaluator::refresh(const Board& board) const {
    Accumulator accumulator;
    for (auto& perspective : accumulator.hidden)
        for (std::size_t row = 0; row < hidden1_count; ++row)
            perspective[row] = nnue_b1_[row];
    for (std::size_t square = 0; square < board.position().squares.size(); ++square)
        accumulate_piece(accumulator, board.position().squares[square], square, 1);
    return accumulator;
}

NeuralEvaluator::Accumulator NeuralEvaluator::update(const Accumulator& parent,
                                                     const Position& before,
                                                     const Position& after) const {
    Accumulator accumulator = parent;
    // Compare actual squares rather than reimplementing the rules here. This
    // also covers both castling pieces, the remote en-passant capture square,
    // and promotion/capture replacement, with no king-dependent refresh.
    for (std::size_t square = 0; square < before.squares.size(); ++square) {
        if (before.squares[square] == after.squares[square]) continue;
        accumulate_piece(accumulator, before.squares[square], square, -1);
        accumulate_piece(accumulator, after.squares[square], square, 1);
    }
    return accumulator;
}

int NeuralEvaluator::evaluate_nnue(const Board& board, const Accumulator& accumulator,
                                  bool king_pawn_shield, bool pawn_structure) const {
    const bool white_turn = board.position().turn == Color::white;
    const auto& perspective = accumulator.hidden[white_turn ? 0 : 1];
    std::array<double, hidden1_count> h1;
    for (std::size_t row = 0; row < hidden1_count; ++row)
        h1[row] = static_cast<double>(perspective[row]) / nnue_scale;
    // Conversion is exact at the permitted magnitude, but the initial weight
    // rounding differs from legacy inference. Its first-layer error is at
    // most (occupied squares + 1) / (2 * nnue_scale); the tail can amplify it.
    const int material = white_turn ? accumulator.material_white : -accumulator.material_white;
    return evaluate_hidden(board, h1, material, king_pawn_shield, pawn_structure);
}

int NeuralEvaluator::evaluate_nnue_full(const Board& board, bool king_pawn_shield,
                                       bool pawn_structure) const {
    return evaluate_nnue(board, refresh(board), king_pawn_shield, pawn_structure);
}

int NeuralEvaluator::evaluate_hidden(const Board& board, std::array<double, hidden1_count>& h1,
                                     int material, bool king_pawn_shield,
                                     bool pawn_structure) const {
    // Sparse binary inputs need additions for occupied features only. Apply
    // ReLU after every feature has been accumulated, as in the original loop.
    for (double& value : h1) value = std::max(0.0, value);
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
    // V2 must preserve even legal promotion-heavy classical scores above 10000.
    // Search separately reserves its mate band for every static evaluator.
    const double score = classical_residual_ ? unbounded_score :
                         std::clamp(unbounded_score, -10000.0, 10000.0);
    return static_cast<int>(std::lround(score));
}
} // namespace regicide
