#include "regicide/evaluation.hpp"
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
bool equal_model(const NeuralModel& a, const NeuralModel& b) {
    return a.input_weights == b.input_weights && a.hidden_bias == b.hidden_bias &&
        a.output_weights == b.output_weights && a.output_scale == b.output_scale;
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
    out << "REGICIDE_NNUE 1 768 16 127 " << model.output_scale << '\n';
    for (int value : model.hidden_bias) out << value << ' ';
    for (int value : model.output_weights) out << value << ' ';
    for (const auto& row : model.input_weights) for (int value : row) out << value << ' ';
    out << "checksum " << neural_model_checksum(model) << '\n';
    return out.str();
}
void exact(const Board& board, const NeuralAccumulator& accumulator,
           const NeuralModel& model, const std::string& label) {
    const auto saved = board;
    expect(accumulator == NeuralAccumulator(board, model), label + ": raw accumulator differs");
    const int score = evaluate_neural(board, model);
    expect(accumulator.evaluate(board.position().turn) == score, label + ": full/incremental score differs");
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
        a.pvs_researches == b.pvs_researches && a.see_calls == b.see_calls && a.tt_bytes == b.tt_bytes,
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
        for (const auto& prefix : {"REGICIDE_NNUE 2 768 16 127 7\n", "REGICIDE_NNUE 1 767 16 127 7\n",
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
                SearchOptions options; options.neural = true; options.neural_model = model;
                options.hash_mb = 1; options.quiescence = quiet; options.fast_moves = fast;
                const auto incremental = search(board, limits, nullptr, {}, options, &context);
                options.neural_incremental = false;
                same_graph(incremental, search(board, limits, nullptr, {}, options, &context));
                options.neural_model.reset();
                same_graph(search(board, limits, nullptr, {}, options, &context),
                           search(board, limits, nullptr, {}, options));
                options.neural_model = model; options.neural_incremental = true;
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
