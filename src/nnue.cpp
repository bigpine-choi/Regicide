#include "regicide/nnue.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <locale>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>

namespace regicide {
namespace {
constexpr int hidden = 16;
constexpr int inputs = 768;
constexpr int base_activation_clip = 127;
constexpr std::size_t maximum_model_bytes = 256 * 1024;
constexpr std::size_t maximum_token_bytes = 64;
// Original deterministic bootstrap weights, model bootstrap-v1. No training data
// or external weights. First six channels count own pieces; others exercise
// square-dependent nonlinear features. Both perspectives share parameters.
constexpr NeuralModel bootstrap_model() {
    NeuralModel result;
    std::uint32_t state = 0x52454731U;
    for (int feature = 0; feature < inputs; ++feature) {
        const int piece = feature / 64;
        if (piece < 6) result.input_weights[feature][piece] = 1;
        for (int h = 6; h < hidden; ++h) {
            state = state * 1664525U + 1013904223U;
            result.input_weights[feature][h] = static_cast<int>((state >> 16) % 7) - 3;
        }
    }
    result.output_weights = {
        100, 320, 330, 500, 900, 0, 1, -1, 1, -1, 1, -1, 1, -1, 1, -1};
    return result;
}
constexpr NeuralModel builtin = bootstrap_model();

bool whitespace(char value) noexcept {
    return value == ' ' || value == '\t' || value == '\n' || value == '\r' ||
           value == '\f' || value == '\v';
}

class ModelReader {
public:
    explicit ModelReader(std::string_view text) : text_(text) {}
    std::string_view token() {
        skip_whitespace();
        const auto start = offset_;
        while (offset_ < text_.size() && !whitespace(text_[offset_])) {
            if (offset_ - start == maximum_token_bytes)
                throw std::runtime_error("neural model token exceeds 64 bytes");
            ++offset_;
        }
        if (start == offset_) throw std::runtime_error("neural model is truncated");
        return text_.substr(start, offset_ - start);
    }
    int integer(int minimum, int maximum) {
        const auto word = token();
        int value = 0;
        const auto result = std::from_chars(word.data(), word.data() + word.size(), value);
        if (result.ec != std::errc{} || result.ptr != word.data() + word.size() ||
            value < minimum || value > maximum)
            throw std::runtime_error("invalid or out-of-range neural model integer");
        return value;
    }
    std::uint64_t checksum() {
        const auto word = token();
        std::uint64_t value = 0;
        const auto result = std::from_chars(word.data(), word.data() + word.size(), value);
        if (result.ec != std::errc{} || result.ptr != word.data() + word.size())
            throw std::runtime_error("invalid neural model checksum");
        return value;
    }
    void finish() {
        skip_whitespace();
        if (offset_ != text_.size()) throw std::runtime_error("neural model has trailing data");
    }
private:
    void skip_whitespace() noexcept {
        while (offset_ < text_.size() && whitespace(text_[offset_])) ++offset_;
    }
    std::string_view text_;
    std::size_t offset_ = 0;
};

void validate_model(const NeuralModel& model) {
    if (model.format_version != 1 && model.format_version != 2)
        throw std::runtime_error("unsupported neural model version");
    if (model.format_version == 1 && model.input_scale != 1)
        throw std::runtime_error("neural model v1 input scale must be 1");
    if (model.format_version == 2 &&
        (model.input_scale < 2 || model.input_scale > 256 ||
         (model.input_scale & (model.input_scale - 1)) != 0))
        throw std::runtime_error("neural model v2 input scale must be a power of two in [2, 256]");
    if (model.output_scale < 1 || model.output_scale > 65536)
        throw std::runtime_error("neural model output scale must be in [1, 65536]");
    if (model.output_scale % model.input_scale != 0)
        throw std::runtime_error("neural model denominator must be divisible by input scale");
    const int bias_bound = model.format_version == 1 ? 4096 : 1048576;
    for (int value : model.hidden_bias)
        if (value < -bias_bound || value > bias_bound)
            throw std::runtime_error("neural model hidden bias is out of range");
    for (int value : model.output_weights)
        if (value < -8192 || value > 8192)
            throw std::runtime_error("neural model output weight must be in [-8192, 8192]");
    const int input_bound = model.format_version == 1 ? 127 : 32767;
    for (const auto& row : model.input_weights)
        for (int value : row)
            if (value < -input_bound || value > input_bound)
                throw std::runtime_error("neural model input weight is out of range");
}

std::int64_t perspective(const Position& position, Color viewer, const NeuralModel& model) noexcept {
    auto accumulator = model.hidden_bias;
    constexpr std::string_view pieces = "PNBRQKpnbrqk";
    for (int square = 0; square < 64; ++square) {
        const auto index = pieces.find(position.squares[square]);
        if (index == std::string_view::npos) continue;
        const int piece = static_cast<int>(index);
        const int relative_piece = viewer == Color::white ? piece : (piece + 6) % 12;
        const int relative_square = viewer == Color::white ? square : square ^ 56;
        const auto& row = model.input_weights[relative_piece * 64 + relative_square];
        for (int h = 0; h < hidden; ++h) accumulator[h] += row[h];
    }
    std::int64_t score = 0;
    const int activation_clip = base_activation_clip * model.input_scale;
    for (int h = 0; h < hidden; ++h)
        score += static_cast<std::int64_t>(std::clamp(accumulator[h], 0, activation_clip)) *
                 model.output_weights[h];
    return score;
}

int final_score(std::int64_t white_numerator, int scale, Color turn) noexcept {
    // C++ integer division truncates toward zero, including negative scores.
    // Divide the white score once before converting to the side to move.
    const auto white = white_numerator / scale;
    const int score = static_cast<int>(std::clamp<std::int64_t>(white, -28000, 28000));
    return turn == Color::white ? score : -score;
}
} // namespace

const NeuralModel& builtin_neural_model() noexcept { return builtin; }

std::uint64_t neural_model_checksum(const NeuralModel& model) noexcept {
    std::uint64_t hash = 14695981039346656037ULL;
    const auto integer = [&hash](int value) {
        auto word = static_cast<std::uint32_t>(value);
        for (int byte = 0; byte < 4; ++byte) {
            hash ^= word & 0xffU;
            hash *= 1099511628211ULL;
            word >>= 8;
        }
    };
    for (int value : {model.format_version, inputs, hidden,
                     base_activation_clip * model.input_scale, model.output_scale})
        integer(value);
    if (model.format_version == 2) integer(model.input_scale);
    for (int value : model.hidden_bias) integer(value);
    for (int value : model.output_weights) integer(value);
    for (const auto& row : model.input_weights)
        for (int value : row) integer(value);
    return hash;
}

std::shared_ptr<const NeuralModel> load_neural_model(const std::string& path) {
    std::ifstream stream(path, std::ios::binary | std::ios::ate);
    if (!stream) throw std::runtime_error("cannot open neural model: " + path);
    const auto length = stream.tellg();
    if (length < 0 || length > static_cast<std::streamoff>(maximum_model_bytes))
        throw std::runtime_error("neural model exceeds 256 KiB or is unreadable");
    std::string contents(static_cast<std::size_t>(length), '\0');
    stream.seekg(0);
    stream.read(contents.data(), static_cast<std::streamsize>(contents.size()));
    if (!stream || stream.peek() != std::char_traits<char>::eof())
        throw std::runtime_error("cannot read complete neural model");
    ModelReader reader(contents);
    if (reader.token() != "REGICIDE_NNUE") throw std::runtime_error("invalid neural model magic");
    const int version = reader.integer(1, 2);
    reader.integer(inputs, inputs);
    reader.integer(hidden, hidden);
    const int clip = reader.integer(base_activation_clip, base_activation_clip * 256);
    auto model = std::make_shared<NeuralModel>();
    model->format_version = version;
    model->output_scale = reader.integer(1, 65536);
    if (version == 2) model->input_scale = reader.integer(2, 256);
    if (clip != base_activation_clip * model->input_scale)
        throw std::runtime_error("neural model activation clip does not match input scale");
    const int bias_bound = version == 1 ? 4096 : 1048576;
    for (int& value : model->hidden_bias) value = reader.integer(-bias_bound, bias_bound);
    for (int& value : model->output_weights) value = reader.integer(-8192, 8192);
    const int input_bound = version == 1 ? 127 : 32767;
    for (auto& row : model->input_weights)
        for (int& value : row) value = reader.integer(-input_bound, input_bound);
    validate_model(*model);
    if (reader.token() != "checksum") throw std::runtime_error("missing neural model checksum");
    if (reader.checksum() != neural_model_checksum(*model))
        throw std::runtime_error("neural model checksum mismatch");
    reader.finish();
    return model;
}

void save_neural_model(const NeuralModel& model, const std::string& path) {
    validate_model(model);
    namespace fs = std::filesystem;
    const fs::path destination(path);
    if (fs::exists(fs::symlink_status(destination)))
        throw std::runtime_error("neural model destination already exists: " + path);
    static std::atomic<std::uint64_t> sequence{0};
    const auto tick = std::chrono::steady_clock::now().time_since_epoch().count();
    auto temporary = destination;
    temporary += ".tmp-" + std::to_string(tick) + "-" + std::to_string(sequence.fetch_add(1));
    struct RemoveTemporary {
        fs::path path;
        ~RemoveTemporary() { std::error_code ignored; fs::remove(path, ignored); }
    } cleanup{temporary};
    std::ofstream stream(temporary, std::ios::binary | std::ios::trunc);
    if (!stream) throw std::runtime_error("cannot create neural model temporary file");
    stream.imbue(std::locale::classic());
    stream << "REGICIDE_NNUE\n" << model.format_version << ' ' << inputs << ' ' << hidden << ' '
           << base_activation_clip * model.input_scale << ' ' << model.output_scale;
    if (model.format_version == 2) stream << ' ' << model.input_scale;
    stream << '\n';
    for (int value : model.hidden_bias) stream << value << ' ';
    stream << '\n';
    for (int value : model.output_weights) stream << value << ' ';
    stream << '\n';
    for (const auto& row : model.input_weights) {
        for (int value : row) stream << value << ' ';
        stream << '\n';
    }
    stream << "checksum " << neural_model_checksum(model) << '\n';
    stream.close();
    if (!stream) throw std::runtime_error("cannot finish neural model temporary file");
    if (fs::exists(fs::symlink_status(destination)))
        throw std::runtime_error("neural model destination already exists: " + path);
    // Publishing a hard link is an atomic no-replace operation on NTFS/POSIX;
    // the completed temporary file is removed by cleanup after publication.
    fs::create_hard_link(temporary, destination);
}

int evaluate_neural(const Board& board) {
    return evaluate_neural(board, builtin_neural_model());
}

int evaluate_neural(const Board& board, const NeuralModel& model) noexcept {
    const auto& position = board.position();
    return final_score(perspective(position, Color::white, model) -
                       perspective(position, Color::black, model), model.output_scale, position.turn);
}

void NeuralAccumulator::add_piece(char piece, int square, int sign) noexcept {
    constexpr std::string_view pieces = "PNBRQKpnbrqk";
    const auto index = pieces.find(piece);
    if (index == std::string_view::npos) return;
    const int type = static_cast<int>(index);
    const auto& white = model_->input_weights[type * 64 + square];
    const auto& black = model_->input_weights[((type + 6) % 12) * 64 + (square ^ 56)];
    for (int h = 0; h < hidden; ++h) {
        sums_[0][h] += sign * white[h];
        sums_[1][h] += sign * black[h];
    }
}

NeuralAccumulator::NeuralAccumulator(const Board& board, const NeuralModel& model) noexcept : model_(&model) {
    sums_[0] = model.hidden_bias;
    sums_[1] = model.hidden_bias;
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
    std::int64_t white = 0;
    const int activation_clip = base_activation_clip * model_->input_scale;
    for (int h = 0; h < hidden; ++h)
        white += static_cast<std::int64_t>(std::clamp(sums_[0][h], 0, activation_clip) -
                                         std::clamp(sums_[1][h], 0, activation_clip)) *
                 model_->output_weights[h];
    return final_score(white, model_->output_scale, turn);
}
} // namespace regicide
