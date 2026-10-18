#include "regicide/neural.hpp"

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
    std::array<std::string, 5> expected{"REGICIDE_NN_V1", "768", "32", "16", "600"};
    for (const auto& token : expected) {
        std::string actual;
        if (!(header >> actual) || actual != token)
            throw std::runtime_error("Unsupported neural model header or dimensions.");
    }
    std::string extra;
    if (header >> extra) throw std::runtime_error("Extra data in neural model header.");

    auto model = std::shared_ptr<NeuralEvaluator>(new NeuralEvaluator);
    read_parameters(data, model->w1_);
    read_parameters(data, model->b1_);
    read_parameters(data, model->w2_);
    read_parameters(data, model->b2_);
    read_parameters(data, model->w3_);
    model->b3_ = read_parameter(data);
    if (data >> extra) throw std::runtime_error("Extra data after neural model parameters.");
    if (data.bad()) throw std::runtime_error("Failed parsing neural model parameters.");
    return model;
}

int NeuralEvaluator::evaluate(const Board& board) const {
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

    // Sparse binary inputs: summing occupied feature columns gives the exact
    // same network as a dense vector with 0/1 entries, without 768 multiplies.
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
    const double score = std::clamp(material + residual_scale * residual, -10000.0, 10000.0);
    return static_cast<int>(std::lround(score));
}
} // namespace regicide
