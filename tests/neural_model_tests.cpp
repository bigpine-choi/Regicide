#include "regicide/evaluation.hpp"
#include "regicide/nnue.hpp"
#include "regicide/search.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

using namespace regicide;
namespace {
int assertions = 0, groups = 0, failures = 0;
void expect(bool condition, const std::string& message) {
    ++assertions;
    if (!condition) throw std::runtime_error(message);
}
template<class Action> void rejects(Action action, const std::string& label) {
    bool rejected = false;
    try { action(); } catch (const std::exception&) { rejected = true; }
    expect(rejected, label + " was accepted");
}
void test(const std::string& name, const std::function<void()>& action) {
    ++groups;
    try { action(); std::cout << "PASS " << name << '\n'; }
    catch (const std::exception& error) {
        ++failures; std::cerr << "FAIL " << name << ": " << error.what() << '\n';
    }
}
struct TemporaryDirectory {
    std::filesystem::path path;
    TemporaryDirectory() {
        const auto suffix = std::chrono::steady_clock::now().time_since_epoch().count();
        path = std::filesystem::temp_directory_path() / ("regicide-neural-model-test-" + std::to_string(suffix));
        if (!std::filesystem::create_directory(path)) throw std::runtime_error("temporary directory collision");
    }
    ~TemporaryDirectory() { std::error_code error; std::filesystem::remove_all(path, error); }
    std::string file(const std::string& name) const { return (path / name).string(); }
};
NeuralModel custom_model() {
    NeuralModel model;
    model.output_scale = 7;
    for (int h = 0; h < 16; ++h) {
        model.hidden_bias[h] = h % 2 == 0 ? 113 : -11;
        model.output_weights[h] = (h % 2 == 0 ? 1 : -1) * (h + 3);
        for (int feature = 0; feature < 768; ++feature)
            model.input_weights[feature][h] = (feature * 17 + h * 31) % 255 - 127;
    }
    // Every legal board exercises both clipping limits, alongside channels that
    // change nonlinearly as pieces move, disappear, promote, or are restored.
    model.hidden_bias[0] = 4096;
    model.hidden_bias[1] = -4096;
    for (auto& row : model.input_weights) { row[0] = 127; row[1] = -127; }
    model.input_weights[0][2] = 127;
    return model;
}
NeuralModel precision_model(int scale) {
    auto model = custom_model();
    model.format_version = 2;
    model.input_scale = scale;
    model.output_scale *= scale;
    for (int& value : model.hidden_bias) value *= scale;
    for (auto& row : model.input_weights) for (int& value : row) value *= scale;
    return model;
}
NeuralModel turn_model() {
    auto model = precision_model(16);
    model.format_version = 3;
    for (int h = 0; h < 16; ++h)
        model.turn_weights[h] = (h % 2 == 0 ? 1 : -1) * (h + 1) * 17;
    return model;
}
bool equal_model(const NeuralModel& a, const NeuralModel& b) {
    return a.input_weights == b.input_weights && a.hidden_bias == b.hidden_bias &&
        a.output_weights == b.output_weights && a.output_scale == b.output_scale &&
        a.format_version == b.format_version && a.input_scale == b.input_scale &&
        a.turn_weights == b.turn_weights;
}
std::string read(const std::string& path) {
    std::ifstream input(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}
void write(const std::string& path, const std::string& text) {
    std::ofstream output(path, std::ios::binary); output << text;
    if (!output) throw std::runtime_error("test file write failed");
}
// Deliberately bypass the public writer to test range checks with a matching
// checksum. A malformed model must not pass merely because its hash matches.
std::string raw_model(const NeuralModel& model) {
    std::ostringstream out;
    out << "REGICIDE_NNUE " << model.format_version << ' '
        << (model.format_version == 3 ? 769 : 768) << " 16 " << 127 * model.input_scale
        << ' ' << model.output_scale;
    if (model.format_version >= 2) out << ' ' << model.input_scale;
    out << '\n';
    for (int value : model.hidden_bias) out << value << ' ';
    for (int value : model.output_weights) out << value << ' ';
    for (const auto& row : model.input_weights) for (int value : row) out << value << ' ';
    if (model.format_version == 3)
        for (int value : model.turn_weights) out << value << ' ';
    out << "checksum " << neural_model_checksum(model) << '\n';
    return out.str();
}
// Historical v1/v2 writer layout, independent of the version-aware writer.
std::string legacy_bytes(const NeuralModel& model) {
    std::ostringstream out;
    out << "REGICIDE_NNUE\n" << model.format_version << " 768 16 " << 127 * model.input_scale
        << ' ' << model.output_scale;
    if (model.format_version == 2) out << ' ' << model.input_scale;
    out << '\n';
    for (int value : model.hidden_bias) out << value << ' ';
    out << '\n';
    for (int value : model.output_weights) out << value << ' ';
    out << '\n';
    for (const auto& row : model.input_weights) {
        for (int value : row) out << value << ' ';
        out << '\n';
    }
    out << "checksum " << neural_model_checksum(model) << '\n';
    return out.str();
}
void exact(const Board& board, const NeuralAccumulator& accumulator,
           const NeuralModel& model, const std::string& label) {
    const auto saved = board;
    expect(accumulator == NeuralAccumulator(board, model), label + ": raw accumulator differs");
    const int score = evaluate_neural(board, model);
    expect(accumulator.evaluate(board.position().turn) == score, label + ": full/incremental score differs");
    if (model.format_version < 3)
        expect(accumulator.evaluate(opposite(board.position().turn)) == -score, label + ": score perspective differs");
    expect(board == saved, label + ": evaluation changed board/history");
}
void apply(Board& board, NeuralAccumulator& accumulator, Move move, bool fast) {
    const Position before = board.position();
    if (fast) board.play_generated(move); else board.play(move);
    accumulator.update_generated(before, board.position(), move);
}
void siblings(Board& board, NeuralAccumulator& accumulator, const NeuralModel& model, bool fast) {
    const auto saved_board = board;
    const auto saved_accumulator = accumulator;
    for (Move move : board.legal_moves()) {
        apply(board, accumulator, move, fast);
        exact(board, accumulator, model, "sibling " + move.uci());
        board.undo(); accumulator = saved_accumulator;
        expect(board == saved_board, "sibling undo changed board/history");
        exact(board, accumulator, model, "sibling undo");
    }
}
void same_graph(const SearchResult& a, const SearchResult& b) {
    expect(a.best_move == b.best_move && a.score == b.score && a.depth == b.depth &&
        a.pv == b.pv && a.stopped == b.stopped, "loaded model search result differs");
    expect(a.nodes == b.nodes && a.qnodes == b.qnodes && a.seldepth == b.seldepth &&
        a.tt_probes == b.tt_probes && a.tt_hits == b.tt_hits && a.tt_cutoffs == b.tt_cutoffs &&
        a.tt_stores == b.tt_stores && a.pvs_probes == b.pvs_probes &&
        a.pvs_researches == b.pvs_researches && a.see_calls == b.see_calls && a.tt_bytes == b.tt_bytes &&
        a.lmr_reductions == b.lmr_reductions && a.lmr_researches == b.lmr_researches,
        "loaded model search graph differs");
}
std::uint32_t random_next(std::uint32_t& state) {
    state ^= state << 13; state ^= state >> 17; state ^= state << 5; return state;
}
} // namespace

int main(int argc, char** argv) {
    TemporaryDirectory temporary;
    test("builtin and custom parameter serialization, checksum and safe save", [&] {
        for (const auto& [name, model] : std::array<std::pair<std::string, NeuralModel>, 2>{{
                {"builtin", builtin_neural_model()}, {"custom", custom_model()}}}) {
            const auto path = temporary.file(name + ".nnue");
            save_neural_model(model, path);
            const auto loaded = load_neural_model(path);
            expect(equal_model(model, *loaded), name + " parameter round trip");
            expect(neural_model_checksum(model) == neural_model_checksum(*loaded), name + " checksum round trip");
            const auto original = read(path);
            rejects([&] { save_neural_model(custom_model(), path); }, "existing model overwrite");
            expect(read(path) == original, "rejected save changed destination");
            const Board board;
            expect(evaluate_neural(board, model) == evaluate_neural(board, *loaded), "round-trip score");
        }
        expect(evaluate_neural(Board()) == evaluate_neural(Board(), builtin_neural_model()), "default model behavior");
    });
    test("malformed headers, checksum, truncation, ranges and trailing data", [&] {
        const auto path = temporary.file("invalid.nnue");
        const auto good = raw_model(custom_model());
        auto malformed = good;
        malformed.replace(0, std::string("REGICIDE_NNUE").size(), "OTHER_FORMAT");
        write(path, malformed); rejects([&] { (void)load_neural_model(path); }, "bad signature");
        for (const auto& prefix : {"REGICIDE_NNUE 3 768 16 127 7\n", "REGICIDE_NNUE 1 767 16 127 7\n",
                                  "REGICIDE_NNUE 1 768 15 127 7\n", "REGICIDE_NNUE 1 768 16 128 7\n"}) {
            write(path, prefix + good.substr(good.find('\n') + 1));
            rejects([&] { (void)load_neural_model(path); }, "unsupported header");
        }
        write(path, good.substr(0, good.size() / 2));
        rejects([&] { (void)load_neural_model(path); }, "truncated model");
        write(path, good + " garbage\n");
        rejects([&] { (void)load_neural_model(path); }, "trailing token");
        auto changed = custom_model(); ++changed.input_weights[12][4];
        auto corrupt = raw_model(changed);
        const auto checksum_start = corrupt.find_last_of(' ', corrupt.size() - 2) + 1;
        corrupt.replace(checksum_start, std::string::npos, std::to_string(neural_model_checksum(custom_model())) + "\n");
        write(path, corrupt); rejects([&] { (void)load_neural_model(path); }, "checksum mismatch");
        for (int kind = 0; kind < 8; ++kind) {
            auto invalid = custom_model();
            if (kind == 0) invalid.output_scale = 0;
            if (kind == 1) invalid.output_scale = 65537;
            if (kind == 2) invalid.hidden_bias[0] = 4097;
            if (kind == 3) invalid.hidden_bias[0] = -4097;
            if (kind == 4) invalid.output_weights[0] = 8193;
            if (kind == 5) invalid.output_weights[0] = -8193;
            if (kind == 6) invalid.input_weights[0][0] = 128;
            if (kind == 7) invalid.input_weights[0][0] = -128;
            write(path, raw_model(invalid));
            rejects([&] { (void)load_neural_model(path); }, "out-of-range parameter");
            const auto save_path = temporary.file("invalid-save-" + std::to_string(kind));
            rejects([&] { save_neural_model(invalid, save_path); }, "invalid parameter save");
            expect(!std::filesystem::exists(save_path), "invalid save left destination");
        }
        rejects([&] { (void)load_neural_model(temporary.file("missing.nnue")); }, "missing file");
    });
    test("fixed-point v2 serialization and exact integer rescaling of v1", [&] {
        const auto original = custom_model();
        expect(neural_model_checksum(original) == 18155381136170626874ULL,
               "historical v1 checksum changed");
        for (int scale = 2; scale <= 256; scale *= 2) {
            const auto fixed = precision_model(scale);
            const auto fixed_path = temporary.file("precision-" + std::to_string(scale) + ".nnue");
            save_neural_model(fixed, fixed_path);
            const auto loaded = load_neural_model(fixed_path);
            expect(equal_model(fixed, *loaded), "v2 parameter round trip");
            expect(neural_model_checksum(fixed) == neural_model_checksum(*loaded), "v2 checksum round trip");
            expect(neural_model_checksum(fixed) != neural_model_checksum(original), "v2 precision absent from checksum");
            const auto serialized = read(fixed_path);
            rejects([&] { save_neural_model(fixed, fixed_path); }, "existing v2 destination overwrite");
            expect(read(fixed_path) == serialized, "v2 rejected save changed destination");
            for (const char* source : {Board::start_fen.data(),
                    "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
                    "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
                    "1r5k/P7/8/8/8/8/8/6K1 w - - 0 1"}) {
                Board board(source); NeuralAccumulator accumulator(board, *loaded);
                expect(evaluate_neural(board, original) == evaluate_neural(board, *loaded),
                       "v2 exact rescaling changed v1 score");
                exact(board, accumulator, *loaded, "rescaled v2 root");
                siblings(board, accumulator, *loaded, true);
            }
        }
    });
    test("v2 malformed precision headers, numeric limits and checksum rejection", [&] {
        const auto invalid_path = temporary.file("invalid-v2.nnue");
        const auto good = raw_model(precision_model(16));
        for (const char* header : {"REGICIDE_NNUE 2 768 16 2031 112 16\n",
                "REGICIDE_NNUE 2 768 16 2032 112 8\n", "REGICIDE_NNUE 2 768 16 381 112 3\n",
                "REGICIDE_NNUE 2 768 16 2032 112 1\n", "REGICIDE_NNUE 2 768 16 65024 4096 512\n",
                "REGICIDE_NNUE 2 768 16 2032 113 16\n", "REGICIDE_NNUE 2 768 16 2032 0 16\n",
                "REGICIDE_NNUE 2 768 16 2032 65537 16\n"}) {
            write(invalid_path, header + good.substr(good.find('\n') + 1));
            rejects([&] { (void)load_neural_model(invalid_path); }, "malformed v2 header");
        }
        for (int kind = 0; kind < 12; ++kind) {
            auto invalid = precision_model(16);
            if (kind == 0) invalid.input_weights[0][0] = 32768;
            if (kind == 1) invalid.input_weights[0][0] = -32768;
            if (kind == 2) invalid.hidden_bias[0] = 1048577;
            if (kind == 3) invalid.hidden_bias[0] = -1048577;
            if (kind == 4) invalid.output_weights[0] = 8193;
            if (kind == 5) invalid.output_weights[0] = -8193;
            if (kind == 6) invalid.output_scale = 65537;
            if (kind == 7) invalid.output_scale = 113;
            if (kind == 8) invalid.input_scale = 3;
            if (kind == 9) invalid.input_scale = 0;
            if (kind == 10) invalid.format_version = 4;
            if (kind == 11) { invalid.format_version = 1; invalid.input_scale = 2; }
            write(invalid_path, raw_model(invalid));
            rejects([&] { (void)load_neural_model(invalid_path); }, "out-of-range v2 parameter load");
            const auto invalid_save = temporary.file("invalid-v2-save-" + std::to_string(kind));
            rejects([&] { save_neural_model(invalid, invalid_save); }, "out-of-range v2 parameter save");
            expect(!std::filesystem::exists(invalid_save), "rejected v2 save left destination");
        }
        auto tampered = good;
        tampered.replace(tampered.find("2032 112 16"), 11, "1016 112 8");
        write(invalid_path, tampered);
        rejects([&] { (void)load_neural_model(invalid_path); }, "valid precision edit with stale checksum");
        write(invalid_path, good.substr(0, good.size() / 2));
        rejects([&] { (void)load_neural_model(invalid_path); }, "truncated v2 model");
        write(invalid_path, good + " trailing");
        rejects([&] { (void)load_neural_model(invalid_path); }, "trailing v2 token");
    });
    test("v2 fractional inputs, one signed division, clipping crossings and undo", [&] {
        const Board pawn("7k/8/8/8/8/8/P7/6K1 w - - 0 1");
        NeuralModel tiny;
        tiny.format_version = 2; tiny.input_scale = 16; tiny.output_scale = 128;
        tiny.input_weights[8][0] = 1; tiny.output_weights[0] = 128;
        expect(evaluate_neural(pawn, tiny) == 1, "fractional 1/16 input vanished from inference");
        exact(pawn, NeuralAccumulator(pawn, tiny), tiny, "fractional positive");
        tiny.output_weights[0] = -128;
        expect(evaluate_neural(pawn, tiny) == -1, "fractional negative score sign");
        tiny.input_scale = 2; tiny.output_scale = 4;
        tiny.input_weights[8][0] = 5; tiny.input_weights[6 * 64 + (8 ^ 56)][0] = 3;
        tiny.output_weights[0] = 1;
        expect(evaluate_neural(pawn, tiny) == 0, "perspectives were divided separately");
        tiny.input_weights[6 * 64 + (8 ^ 56)][0] = 0; tiny.output_weights[0] = -1;
        expect(evaluate_neural(pawn, tiny) == -1, "negative division did not truncate toward zero");
        NeuralModel crossing;
        crossing.format_version = 2; crossing.input_scale = 16; crossing.output_scale = 128;
        crossing.hidden_bias[0] = 2031; crossing.hidden_bias[1] = 1;
        crossing.output_weights[0] = 128; crossing.output_weights[1] = 256;
        crossing.input_weights[12][0] = 2; crossing.input_weights[28][0] = -2;
        crossing.input_weights[12][1] = -2; crossing.input_weights[28][1] = 2;
        Board board("7k/8/8/8/8/8/4P3/6K1 w - - 0 1");
        NeuralAccumulator accumulator(board, crossing);
        const auto saved_board = board; const auto saved_accumulator = accumulator;
        expect(evaluate_neural(board, crossing) == -1, "upper/lower parent clipping arithmetic");
        exact(board, accumulator, crossing, "v2 clipping parent");
        apply(board, accumulator, Move::from_uci("e2e4"), true);
        expect(evaluate_neural(board, crossing) == -2, "upper/lower child clipping arithmetic");
        exact(board, accumulator, crossing, "v2 clipping child");
        board.undo(); accumulator = saved_accumulator;
        expect(board == saved_board, "v2 clipping undo changed board/history");
        exact(board, accumulator, crossing, "v2 clipping undo");
        NeuralModel extreme;
        extreme.format_version = 2; extreme.input_scale = 256; extreme.output_scale = 256;
        for (int h = 0; h < 16; ++h) {
            extreme.output_weights[h] = 8192;
            for (int feature = 0; feature < 768; ++feature)
                extreme.input_weights[feature][h] = feature < 384 ? 32767 : -32767;
        }
        expect(evaluate_neural(pawn, extreme) == 28000, "wide v2 numerator overflow or clamp");
        exact(pawn, NeuralAccumulator(pawn, extreme), extreme, "v2 wide numerator");
    });
    test("v3 serialization, legacy bytes and zero-feature compatibility", [&] {
        expect(neural_model_checksum(precision_model(16)) == 8263884807544471307ULL,
               "historical v2 checksum changed");
        for (int version : {1, 2}) {
            const auto legacy = version == 1 ? custom_model() : precision_model(16);
            const auto legacy_path = temporary.file("legacy-bytes-" + std::to_string(version));
            save_neural_model(legacy, legacy_path);
            expect(read(legacy_path) == legacy_bytes(legacy), "legacy serialized bytes changed");
            auto empty_turn = version == 1 ? precision_model(16) : legacy;
            empty_turn.format_version = 3;
            for (const char* source : {Board::start_fen.data(),
                    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1",
                    "7k/8/8/8/8/8/P7/6K1 w - - 0 1"}) {
                const Board board(source);
                expect(evaluate_neural(board, legacy) == evaluate_neural(board, empty_turn),
                       "zero v3 turn feature changed legacy evaluation");
                exact(board, NeuralAccumulator(board, empty_turn), empty_turn, "zero v3 turn feature");
            }
            auto hidden_turn = legacy;
            hidden_turn.turn_weights[3] = 1;
            const auto invalid_path = temporary.file("legacy-hidden-turn-" + std::to_string(version));
            rejects([&] { save_neural_model(hidden_turn, invalid_path); }, "legacy hidden turn weight");
            expect(!std::filesystem::exists(invalid_path), "legacy invalid save left destination");
        }
        const auto original = turn_model();
        const auto v3_path = temporary.file("turn-v3.nnue");
        save_neural_model(original, v3_path);
        const auto loaded = load_neural_model(v3_path);
        expect(equal_model(original, *loaded), "v3 parameter round trip");
        expect(read(v3_path).starts_with("REGICIDE_NNUE\n3 769 16 2032 112 16\n"),
               "v3 header does not declare the turn input");
        const auto serialized = read(v3_path);
        rejects([&] { save_neural_model(original, v3_path); }, "existing v3 destination overwrite");
        expect(read(v3_path) == serialized, "rejected v3 save changed destination");
        auto changed = original;
        ++changed.turn_weights[5];
        expect(neural_model_checksum(original) != neural_model_checksum(changed),
               "turn input is absent from checksum");
        expect(evaluate_neural(Board(), original) == evaluate_neural(Board(), *loaded),
               "v3 round-trip score");
    });
    test("v3 turn feature, clipping and unchanged-piece turn flip", [&] {
        NeuralModel tempo;
        tempo.format_version = 3; tempo.input_scale = 16; tempo.output_scale = 128;
        tempo.hidden_bias[0] = 10; tempo.output_weights[0] = 128; tempo.turn_weights[0] = 32;
        const Board white("7k/8/8/8/8/8/P7/6K1 w - - 0 1");
        const Board black("7k/8/8/8/8/8/P7/6K1 b - - 0 1");
        const NeuralAccumulator accumulator(white, tempo);
        expect(white.position().squares == black.position().squares, "turn flip changed pieces");
        expect(accumulator == NeuralAccumulator(black, tempo), "turn feature leaked into stored piece sums");
        expect(evaluate_neural(white, tempo) == 32 && evaluate_neural(black, tempo) == 32,
               "turn feature was not relative to the moving perspective");
        expect(accumulator.evaluate(Color::white) == 32 && accumulator.evaluate(Color::black) == 32,
               "turn-only change requires a piece update");
        exact(white, accumulator, tempo, "white tempo");
        exact(black, accumulator, tempo, "black tempo");
        tempo.hidden_bias[0] = 2031; tempo.turn_weights[0] = 2;
        tempo.hidden_bias[1] = 1; tempo.turn_weights[1] = -2; tempo.output_weights[1] = 256;
        expect(evaluate_neural(white, tempo) == -1 && evaluate_neural(black, tempo) == -1,
               "turn weight must be added before upper/lower clipping");
        exact(white, NeuralAccumulator(white, tempo), tempo, "turn clipping white");
        exact(black, NeuralAccumulator(black, tempo), tempo, "turn clipping black");
        tempo.hidden_bias.fill(0); tempo.turn_weights.fill(32767); tempo.output_weights.fill(8192);
        expect(evaluate_neural(white, tempo) == 28000, "v3 wide numerator overflow or clamp");
        exact(white, NeuralAccumulator(white, tempo), tempo, "wide v3 turn numerator");
    });
    test("v3 header, turn ranges, truncation and checksum rejection", [&] {
        const auto invalid_path = temporary.file("invalid-v3.nnue");
        const auto original = turn_model();
        const auto good = raw_model(original);
        for (const char* header : {"REGICIDE_NNUE 3 768 16 2032 112 16\n",
                "REGICIDE_NNUE 3 770 16 2032 112 16\n", "REGICIDE_NNUE 3 769 16 2031 112 16\n",
                "REGICIDE_NNUE 3 769 16 2032 112 8\n", "REGICIDE_NNUE 3 769 16 381 112 3\n",
                "REGICIDE_NNUE 3 769 16 127 112 1\n", "REGICIDE_NNUE 4 769 16 2032 112 16\n"}) {
            write(invalid_path, header + good.substr(good.find('\n') + 1));
            rejects([&] { (void)load_neural_model(invalid_path); }, "malformed v3 header");
        }
        for (int kind = 0; kind < 4; ++kind) {
            auto invalid = original;
            if (kind == 0) invalid.turn_weights[0] = 32768;
            if (kind == 1) invalid.turn_weights[0] = -32768;
            if (kind == 2) invalid.hidden_bias[0] = 1048577;
            if (kind == 3) invalid.input_weights[0][0] = -32768;
            write(invalid_path, raw_model(invalid));
            rejects([&] { (void)load_neural_model(invalid_path); }, "out-of-range v3 load");
            const auto invalid_save = temporary.file("invalid-v3-save-" + std::to_string(kind));
            rejects([&] { save_neural_model(invalid, invalid_save); }, "out-of-range v3 save");
            expect(!std::filesystem::exists(invalid_save), "rejected v3 save left destination");
        }
        auto changed = original;
        ++changed.turn_weights[15];
        auto corrupt = raw_model(changed);
        const auto checksum_start = corrupt.find("checksum ");
        corrupt.replace(checksum_start, std::string::npos,
                        "checksum " + std::to_string(neural_model_checksum(original)) + "\n");
        write(invalid_path, corrupt);
        rejects([&] { (void)load_neural_model(invalid_path); }, "turn edit with stale checksum");
        const auto row_end = good.find("checksum ");
        write(invalid_path, good.substr(0, row_end - 4));
        rejects([&] { (void)load_neural_model(invalid_path); }, "truncated turn row");
        write(invalid_path, good + " trailing");
        rejects([&] { (void)load_neural_model(invalid_path); }, "trailing v3 data");
    });
    test("v3 special moves, exact undo and full/incremental search", [&] {
        const auto v3_path = temporary.file("turn-walk-v3.nnue");
        save_neural_model(turn_model(), v3_path);
        const auto model = load_neural_model(v3_path);
        std::vector<std::pair<std::string, std::string>> fixtures{
            {"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"},
            {"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1c1"},
            {"r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8g8"},
            {"r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8"},
            {"7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1", "e5d6"},
            {"7k/8/8/8/3pP3/8/8/6K1 b - e3 0 1", "d4e3"},
            {std::string(Board::start_fen), "e2e4"}};
        for (char promotion : std::string("qrbn")) {
            fixtures.emplace_back("7k/P7/8/8/8/8/8/6K1 w - - 0 1", "a7a8" + std::string(1, promotion));
            fixtures.emplace_back("1r5k/P7/8/8/8/8/8/6K1 w - - 0 1", "a7b8" + std::string(1, promotion));
            fixtures.emplace_back("7k/8/8/8/8/8/p7/6K1 b - - 0 1", "a2a1" + std::string(1, promotion));
            fixtures.emplace_back("7k/8/8/8/8/8/p7/1R4K1 b - - 0 1", "a2b1" + std::string(1, promotion));
        }
        for (const auto& [source, text] : fixtures) for (bool fast : {false, true}) {
            Board board(source); NeuralAccumulator accumulator(board, *model);
            const auto saved_board = board; const auto saved_accumulator = accumulator;
            exact(board, accumulator, *model, "v3 special parent");
            apply(board, accumulator, Move::from_uci(text), fast);
            exact(board, accumulator, *model, "v3 special " + text);
            siblings(board, accumulator, *model, fast);
            board.undo(); accumulator = saved_accumulator;
            expect(board == saved_board, "v3 special undo changed board/history");
            exact(board, accumulator, *model, "v3 special undo");
        }
        Board board; NeuralAccumulator accumulator(board, *model);
        const auto root = board; const auto root_accumulator = accumulator;
        std::vector<std::pair<Board, NeuralAccumulator>> parents;
        std::uint32_t seed = 0x53544d33U;
        for (int ply = 0; ply < 120; ++ply) {
            const auto moves = board.legal_moves(); if (moves.empty()) break;
            parents.emplace_back(board, accumulator);
            apply(board, accumulator, moves[random_next(seed) % moves.size()], ply % 2 == 0);
            exact(board, accumulator, *model, "v3 legal walk");
        }
        expect(parents.size() >= 32, "v3 legal walk too short");
        while (!parents.empty()) {
            board.undo(); accumulator = parents.back().second;
            expect(board == parents.back().first, "v3 walk undo board/history");
            exact(board, accumulator, *model, "v3 walk undo"); parents.pop_back();
        }
        expect(board == root && accumulator == root_accumulator, "v3 complete unwind changed root");
        SearchLimits limits; limits.depth = 2;
        SearchOptions options; options.nnue = true; options.nnue_model = model; options.hash_mb = 1;
        for (const char* source : {Board::start_fen.data(),
                "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
                "1r5k/P7/8/8/8/8/8/6K1 w - - 0 1"}) {
            const Board position(source);
            options.nnue_incremental = true;
            const auto incremental = search(position, limits, nullptr, {}, options);
            options.nnue_incremental = false;
            same_graph(incremental, search(position, limits, nullptr, {}, options));
        }
    });
    const auto path = temporary.file("walk.nnue");
    save_neural_model(custom_model(), path);
    // The pipeline integration supplies the freshly trained export here. With
    // no argument the independently constructed nonlinear stress model is used.
    const auto model_path = argc > 1 ? std::string(argv[1]) : path;
    const auto model = load_neural_model(model_path);
    test("loaded model identity and snapshot model references", [&] {
        Board board;
        const auto second = load_neural_model(model_path);
        NeuralAccumulator first(board, *model), other(board, *second);
        const auto snapshot = first;
        expect(!(first == other), "different model identities compare equal");
        expect(first.evaluate(Color::white) == other.evaluate(Color::white), "same weights score differently");
        const Position before = board.position(); board.play("e2e4");
        first.update_generated(before, board.position(), Move::from_uci("e2e4"));
        exact(board, first, *model, "snapshot child");
        board.undo(); first = snapshot; exact(board, first, *model, "snapshot restore");
        first = other; exact(board, first, *second, "cross-model assignment");
    });
    test("loaded weights: all special moves, sibling branches and undo", [&] {
        std::vector<std::pair<std::string, std::string>> fixtures{
            {"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"},
            {"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1c1"},
            {"r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8g8"},
            {"r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8"},
            {"7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1", "e5d6"},
            {"7k/8/8/8/3pP3/8/8/6K1 b - e3 0 1", "d4e3"},
            {std::string(Board::start_fen), "e2e4"},
            {"r3k2r/1P6/8/8/8/8/8/R3K2R w KQkq - 0 1", "b7a8q"}};
        for (char promotion : std::string("qrbn")) {
            fixtures.emplace_back("7k/P7/8/8/8/8/8/6K1 w - - 0 1", "a7a8" + std::string(1, promotion));
            fixtures.emplace_back("1r5k/P7/8/8/8/8/8/6K1 w - - 0 1", "a7b8" + std::string(1, promotion));
            fixtures.emplace_back("7k/8/8/8/8/8/p7/6K1 b - - 0 1", "a2a1" + std::string(1, promotion));
            fixtures.emplace_back("7k/8/8/8/8/8/p7/1R4K1 b - - 0 1", "a2b1" + std::string(1, promotion));
        }
        for (const auto& [source, text] : fixtures) for (bool fast : {false, true}) {
            Board board(source); NeuralAccumulator accumulator(board, *model);
            const auto saved_board = board; const auto saved_accumulator = accumulator;
            const auto moves = board.legal_moves(); const auto move = Move::from_uci(text);
            expect(std::find(moves.begin(), moves.end(), move) != moves.end(), "illegal special fixture");
            exact(board, accumulator, *model, "special parent");
            apply(board, accumulator, move, fast); exact(board, accumulator, *model, "special " + text);
            siblings(board, accumulator, *model, fast);
            board.undo(); accumulator = saved_accumulator;
            expect(board == saved_board, "special undo board/history");
            exact(board, accumulator, *model, "special undo " + text);
        }
    });
    test("loaded weights: seeded legal walks and complete snapshot unwind", [&] {
        for (const char* source : {Board::start_fen.data(),
                "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
                "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1"}) {
            for (std::uint32_t seed : {0x52454731U, 0x4e4e5545U}) {
                Board board(source); NeuralAccumulator accumulator(board, *model);
                const auto root = board; const auto root_accumulator = accumulator;
                std::vector<std::pair<Board, NeuralAccumulator>> parents;
                for (int ply = 0; ply < 120; ++ply) {
                    exact(board, accumulator, *model, "walk parent");
                    const auto moves = board.legal_moves(); if (moves.empty()) break;
                    if (ply % 20 == 0) siblings(board, accumulator, *model, ply % 2 == 0);
                    const Move move = moves[random_next(seed) % moves.size()];
                    parents.emplace_back(board, accumulator);
                    apply(board, accumulator, move, ply % 2 == 0);
                    exact(board, accumulator, *model, "walk child");
                }
                expect(parents.size() >= 32, "loaded-model walk too short");
                while (!parents.empty()) {
                    board.undo(); accumulator = parents.back().second;
                    expect(board == parents.back().first, "deep undo board/history");
                    exact(board, accumulator, *model, "deep undo"); parents.pop_back();
                }
                expect(board == root && accumulator == root_accumulator, "complete unwind did not restore root");
            }
        }
    });
    test("loaded weights: exact full/incremental search, model and context switching", [&] {
        SearchLimits limits; limits.depth = 2; SearchContext context;
        for (const char* source : {Board::start_fen.data(),
                "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
                "1r5k/P7/8/8/8/8/8/6K1 w - - 0 1"}) {
            Board board(source); const auto saved = board;
            for (bool quiet : {false, true}) for (bool fast : {false, true}) {
                SearchOptions options; options.nnue = true; options.nnue_model = model;
                options.hash_mb = 1; options.quiescence = quiet; options.fast_moves = fast;
                const auto incremental = search(board, limits, nullptr, {}, options, &context);
                options.nnue_incremental = false;
                same_graph(incremental, search(board, limits, nullptr, {}, options, &context));
                options.nnue_model.reset();
                same_graph(search(board, limits, nullptr, {}, options, &context),
                           search(board, limits, nullptr, {}, options));
                options.nnue_model = model; options.nnue_incremental = true;
                same_graph(incremental, search(board, limits, nullptr, {}, options, &context));
                std::atomic_bool stop{true};
                expect(search(board, limits, &stop, {}, options).score == evaluate_neural(board, *model),
                       "cancelled fallback ignored loaded weights");
                expect(board == saved, "loaded-model search changed board/history");
            }
        }
    });
    std::cout << "tested_model_checksum " << neural_model_checksum(*model) << '\n';
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
