#include "regicide/neural.hpp"
#include "regicide/evaluation.hpp"
#include "regicide/search.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <functional>
#include <iomanip>
#include <iostream>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace regicide;
namespace {
int groups = 0, assertions = 0, failures = 0;
void expect(bool condition, const std::string& message) {
    ++assertions;
    if (!condition) throw std::runtime_error(message);
}
void test(const std::string& name, const std::function<void()>& action) {
    ++groups;
    try { action(); std::cout << "PASS " << name << '\n'; }
    catch (const std::exception& error) {
        ++failures;
        std::cerr << "FAIL " << name << ": " << error.what() << '\n';
    }
}

constexpr std::size_t b1 = 768 * 32;
constexpr std::size_t w2 = b1 + 32;
constexpr std::size_t b2 = w2 + 32 * 16;
constexpr std::size_t w3 = b2 + 16;
constexpr std::size_t b3 = w3 + 16;
constexpr std::int64_t scale = std::int64_t{1} << 24;
using Parameters = std::vector<double>;
using Hidden = std::array<std::array<std::int64_t, 32>, 2>;

Parameters zero() { return Parameters(NeuralEvaluator::parameter_count, 0.0); }
std::shared_ptr<const NeuralEvaluator> model(const Parameters& parameters, bool classical = false) {
    std::ostringstream text;
    text << (classical ? "REGICIDE_NN_V2 768 32 16 600 100\n" : "REGICIDE_NN_V1 768 32 16 600\n")
         << std::setprecision(17);
    for (double parameter : parameters) text << parameter << '\n';
    std::istringstream input(text.str());
    return NeuralEvaluator::load(input);
}

// This deliberately reads disk-layout parameters directly. It does not call
// refresh(), update(), or any production feature/quantization helper.
struct Reference {
    Hidden hidden{};
    int material_white = 0;
};
Reference reference_accumulator(const Parameters& parameters, const Board& board) {
    Reference result;
    constexpr std::array<int, 6> value{100, 320, 330, 500, 900, 0};
    constexpr std::array<char, 6> letters{'p', 'n', 'b', 'r', 'q', 'k'};
    for (int perspective = 0; perspective < 2; ++perspective) {
        for (std::size_t neuron = 0; neuron < 32; ++neuron)
            result.hidden[static_cast<std::size_t>(perspective)][neuron] =
                std::llround(parameters[b1 + neuron] * static_cast<double>(scale));
        for (int square = 0; square < 64; ++square) {
            const char piece = board.position().squares[static_cast<std::size_t>(square)];
            if (piece == '.') continue;
            const bool white = piece >= 'A' && piece <= 'Z';
            const char lower = white ? static_cast<char>(piece - 'A' + 'a') : piece;
            const auto found = std::find(letters.begin(), letters.end(), lower);
            expect(found != letters.end(), "unexpected test piece");
            const auto type = static_cast<std::size_t>(found - letters.begin());
            const bool own = white == (perspective == 0);
            const auto channel = type + (own ? 0U : 6U);
            const auto oriented_square = static_cast<std::size_t>(perspective == 0 ? square : square ^ 56);
            const auto feature = channel * 64 + oriented_square;
            for (std::size_t neuron = 0; neuron < 32; ++neuron)
                result.hidden[static_cast<std::size_t>(perspective)][neuron] +=
                    std::llround(parameters[neuron * 768 + feature] * static_cast<double>(scale));
            if (perspective == 0) result.material_white += (white ? 1 : -1) * value[type];
        }
    }
    return result;
}
int reference_score(const Parameters& parameters, bool classical, const Board& board,
                    bool shield = true, bool structure = true) {
    const auto sums = reference_accumulator(parameters, board);
    const std::size_t perspective = board.position().turn == Color::white ? 0 : 1;
    std::array<double, 32> first{};
    for (std::size_t neuron = 0; neuron < 32; ++neuron)
        first[neuron] = std::max(0.0, static_cast<double>(sums.hidden[perspective][neuron]) /
                                       static_cast<double>(scale));
    std::array<double, 16> second{};
    for (std::size_t neuron = 0; neuron < 16; ++neuron) {
        double sum = parameters[b2 + neuron];
        for (std::size_t input = 0; input < 32; ++input)
            sum += parameters[w2 + neuron * 32 + input] * first[input];
        second[neuron] = std::max(0.0, sum);
    }
    double residual = parameters[b3];
    for (std::size_t input = 0; input < 16; ++input)
        residual += parameters[w3 + input] * second[input];
    const int material = perspective == 0 ? sums.material_white : -sums.material_white;
    const double score = classical ? evaluate_position(board, shield, structure) + 100 * std::tanh(residual) :
                                    std::clamp(material + 600 * residual, -10000.0, 10000.0);
    return static_cast<int>(std::lround(score));
}
void verify(const NeuralEvaluator& evaluator, const Parameters& parameters, bool classical,
            const Board& board, const NeuralEvaluator::Accumulator& accumulator,
            const std::string& label, bool all_options = false) {
    const Board saved = board;
    const auto expected = reference_accumulator(parameters, board);
    expect(accumulator.hidden == expected.hidden, label + ": independent integer sums differ");
    expect(accumulator.material_white == expected.material_white, label + ": material differs");
    expect(accumulator == evaluator.refresh(board), label + ": incremental differs from refresh");
    for (bool shield : {false, true}) {
        for (bool structure : {false, true}) {
            if (!all_options && (!shield || !structure)) continue;
            const int score = reference_score(parameters, classical, board, shield, structure);
            expect(evaluator.evaluate_nnue(board, accumulator, shield, structure) == score,
                   label + ": incremental score differs from independent reference");
            expect(evaluator.evaluate_nnue_full(board, shield, structure) == score,
                   label + ": full score differs from independent reference");
        }
    }
    expect(board == saved, label + ": evaluation changed board/history");
}
Parameters sampled(std::uint32_t seed) {
    Parameters parameters = zero();
    std::mt19937 rng(seed);
    std::uniform_real_distribution<double> random(-0.08, 0.08);
    for (double& parameter : parameters) parameter = random(rng);
    return parameters;
}

struct Transition { std::string fen, move; };
std::vector<Transition> special_transitions() {
    std::vector<Transition> result{
        {std::string(Board::start_fen), "e2e4"},
        {"4k3/8/3n4/4P3/8/8/8/4K3 w - - 0 1", "e5d6"},
        {"4k3/8/8/8/4p3/3N4/8/4K3 b - - 0 1", "e4d3"},
        {"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"},
        {"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1c1"},
        {"r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8g8"},
        {"r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8"},
        {"4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6"},
        {"4k3/8/8/8/3Pp3/8/8/4K3 b - d3 0 1", "e4d3"},
        {"4k3/8/8/8/8/8/4P3/4K3 w - - 0 1", "e1d1"},
        {"4k3/4p3/8/8/8/8/8/4K3 b - - 0 1", "e8d8"}};
    for (char promotion : {'q', 'r', 'b', 'n'}) {
        result.push_back({"7k/P7/8/8/8/8/8/4K3 w - - 0 1", std::string("a7a8") + promotion});
        result.push_back({"1r5k/P7/8/8/8/8/8/4K3 w - - 0 1", std::string("a7b8") + promotion});
        result.push_back({"4k3/8/8/8/8/8/p7/7K b - - 0 1", std::string("a2a1") + promotion});
        result.push_back({"4k3/8/8/8/8/8/p7/1R5K b - - 0 1", std::string("a2b1") + promotion});
    }
    return result;
}

int independent_minimax(Board& board, const Parameters& parameters, bool classical, int depth, int ply = 0) {
    const auto outcome = board.outcome();
    if (outcome == Outcome::checkmate) return -mate_score + ply;
    if (outcome != Outcome::ongoing) return 0;
    if (!depth) return reference_score(parameters, classical, board);
    int best = -32000;
    for (Move move : board.legal_moves()) {
        board.play(move);
        const int score = -independent_minimax(board, parameters, classical, depth - 1, ply + 1);
        board.undo();
        best = std::max(best, score);
    }
    return best;
}
int independent_quiescence(Board& board, const Parameters& parameters, bool classical,
                           int ply = 1, int qply = 0) {
    const auto outcome = board.outcome();
    if (outcome == Outcome::checkmate) return -mate_score + ply;
    if (outcome != Outcome::ongoing) return 0;
    expect(qply < 16, "quiescence oracle fixture is unexpectedly deep");
    const bool checked = board.in_check();
    int best = checked ? -32000 : reference_score(parameters, classical, board);
    const auto position = board.position();
    for (Move move : board.legal_moves()) {
        const char piece = position.squares[static_cast<std::size_t>(move.from)];
        const bool ep = (piece == 'P' || piece == 'p') && move.to == position.en_passant;
        if (!checked && position.squares[static_cast<std::size_t>(move.to)] == '.' && !ep && !move.promotion)
            continue;
        board.play(move);
        const int score = -independent_quiescence(board, parameters, classical, ply + 1, qply + 1);
        board.undo();
        best = std::max(best, score);
    }
    return best;
}
void same_search(const SearchResult& first, const SearchResult& second) {
    expect(first.best_move == second.best_move && first.score == second.score &&
               first.depth == second.depth && first.pv == second.pv && first.nodes == second.nodes &&
               first.qnodes == second.qnodes && first.stopped == second.stopped,
           "reused context changed move, score, PV or search graph");
}
} // namespace

int main() {
    test("independent disk-layout reference, perspective mapping and signed half rounding", [] {
        auto parameters = sampled(0x4e4e5545U);
        // These exact binary half steps expose ties-to-even and truncation.
        for (std::size_t neuron = 0; neuron < 8; ++neuron) {
            parameters[b1 + neuron] = (static_cast<double>(neuron) - 3.5) / static_cast<double>(scale);
            for (std::size_t feature = 0; feature < 768; ++feature)
                parameters[neuron * 768 + feature] =
                    (static_cast<double>((feature + neuron) % 8) - 3.5) / static_cast<double>(scale);
        }
        for (bool classical : {false, true}) {
            const auto evaluator = model(parameters, classical);
            for (const char* fen : {"4k3/1p6/8/3n4/4N3/8/6P1/4K3 w - - 0 1",
                                    "4k3/1p6/8/3n4/4N3/8/6P1/4K3 b - - 0 1"}) {
                const Board board(fen);
                verify(*evaluator, parameters, classical, board, evaluator->refresh(board), "perspective", true);
            }
        }
    });
    test("captures, king moves, both castles, both en-passant colors and all 16 promotions", [] {
        const auto parameters = sampled(20261019);
        for (bool classical : {false, true}) {
            const auto evaluator = model(parameters, classical);
            for (const auto& fixture : special_transitions()) {
                Board board(fixture.fen);
                const Board saved = board;
                const auto parent = evaluator->refresh(board);
                const auto parent_copy = parent;
                const auto before = board.position();
                board.play(fixture.move);
                const auto child = evaluator->update(parent, before, board.position());
                verify(*evaluator, parameters, classical, board, child, fixture.move, true);
                expect(parent == parent_copy, fixture.move + ": child update mutated parent cache");
                board.undo();
                expect(board == saved, fixture.move + ": undo changed parent board/history");
                verify(*evaluator, parameters, classical, board, parent, fixture.move + " undo");
                const auto siblings = board.legal_moves();
                expect(!siblings.empty(), "special fixture has no sibling");
                const auto sibling_before = board.position();
                board.play_generated(siblings.front());
                verify(*evaluator, parameters, classical, board,
                       evaluator->update(parent, sibling_before, board.position()), fixture.move + " sibling");
            }
        }
    });
    test("seeded long walks, copy-on-ply undo and repeated sibling updates have no drift", [] {
        const auto parameters = sampled(0x51a7eU);
        const auto evaluator = model(parameters, true);
        std::mt19937 rng(0x6e6e7565U);
        for (int game = 0; game < 6; ++game) {
            Board board;
            const Board initial = board;
            std::vector<NeuralEvaluator::Accumulator> stack{evaluator->refresh(board)};
            for (int ply = 0; ply < 128; ++ply) {
                const auto moves = board.legal_moves();
                if (moves.empty() || board.outcome() != Outcome::ongoing) break;
                if (ply % 11 == 0) {
                    const Board saved = board;
                    const auto parent = stack.back();
                    for (std::size_t sibling = 0; sibling < std::min<std::size_t>(moves.size(), 5); ++sibling) {
                        const auto before = board.position();
                        board.play_generated(moves[sibling]);
                        verify(*evaluator, parameters, true, board,
                               evaluator->update(parent, before, board.position()), "random sibling");
                        board.undo();
                        expect(board == saved && stack.back() == parent, "sibling changed parent state");
                    }
                }
                const auto before = board.position();
                board.play_generated(moves[rng() % moves.size()]);
                stack.push_back(evaluator->update(stack.back(), before, board.position()));
                verify(*evaluator, parameters, true, board, stack.back(), "random walk");
            }
            while (stack.size() > 1) {
                board.undo();
                stack.pop_back();
                verify(*evaluator, parameters, true, board, stack.back(), "random undo");
            }
            expect(board == initial, "long walk failed to restore full initial board");
        }
    });
    test("unclipped first-layer cache survives ReLU crossings in both perspectives", [] {
        auto parameters = zero();
        parameters[b1] = -0.5;
        parameters[12] = 1.0; // White view: own e2 pawn.
        parameters[28] = 0.0; // Its e4 destination falls below the threshold.
        parameters[6 * 64 + (12 ^ 56)] = 1.0; // Black view of the same pawn.
        parameters[w2] = 1.0;
        parameters[w3] = 0.1;
        const auto evaluator = model(parameters);
        Board board("4k3/8/8/8/8/8/4P3/4K3 w - - 0 1");
        const auto parent = evaluator->refresh(board);
        const auto before = board.position();
        board.play("e2e4");
        const auto child = evaluator->update(parent, before, board.position());
        for (std::size_t side = 0; side < 2; ++side) {
            expect(parent.hidden[side][0] == scale / 2, "positive preactivation fixture changed");
            expect(child.hidden[side][0] == -scale / 2, "negative preactivation was clipped in cache");
        }
        verify(*evaluator, parameters, false, board, child, "ReLU crossing");
        board.undo();
        verify(*evaluator, parameters, false, board, parent, "ReLU restored");
    });
    test("allowed parameter extrema use wide sums and retain bounded output", [] {
        for (double sign : {-1.0, 1.0}) {
            Parameters parameters(NeuralEvaluator::parameter_count, sign * 100.0);
            for (bool classical : {false, true}) {
                const auto evaluator = model(parameters, classical);
                Board board;
                const auto parent = evaluator->refresh(board);
                for (const auto& hidden : parent.hidden)
                    for (auto sum : hidden)
                        expect(sum == static_cast<std::int64_t>(sign) * 33 * 100 * scale,
                               "maximum-density integer accumulation overflowed or changed sign");
                verify(*evaluator, parameters, classical, board, parent, "parameter extrema", true);
                const auto before = board.position();
                board.play("e2e4");
                verify(*evaluator, parameters, classical, board,
                       evaluator->update(parent, before, board.position()), "extreme update");
            }
        }
    });
    test("search matches independent integer minimax with PVS and move paths toggled", [] {
        const auto parameters = sampled(314159);
        for (bool classical : {false, true}) {
            const auto evaluator = model(parameters, classical);
            for (const char* fen : {"4k3/2p5/8/3n4/4N3/8/5P2/4K3 w - - 0 1",
                                    "4k3/2p5/8/3n4/4N3/8/5P2/4K3 b - - 0 1"}) {
                Board board(fen);
                const Board saved = board;
                const int oracle = independent_minimax(board, parameters, classical, 2);
                for (bool fast : {false, true}) {
                    for (bool pvs : {false, true}) {
                        SearchOptions options;
                        options.neural = evaluator;
                        options.nnue = true;
                        options.quiescence = false;
                        options.fast_moves = fast;
                        options.pvs = pvs;
                        options.lmr = false;
                        SearchLimits limits;
                        limits.depth = 2;
                        const auto result = search(board, limits, nullptr, {}, options);
                        expect(result.score == oracle && result.depth == 2 && !result.stopped,
                               "search differs from independent quantized minimax");
                        expect(board == saved, "search changed input board/history");
                    }
                }
            }
        }
    });
    test("quiescence special moves match independent unpruned integer evaluation", [] {
        const auto parameters = sampled(271828);
        const std::array<Transition, 6> fixtures{{
            {"4k3/3p4/8/4P3/8/8/8/4K3 b - - 0 1", "d7d5"},
            {"4k3/8/8/8/4p3/8/3P4/4K3 w - - 0 1", "d2d4"},
            {"7k/P7/8/8/8/8/8/4K3 b - - 0 1", "h8h7"},
            {"4k3/8/8/8/8/8/p7/7K w - - 0 1", "h1h2"},
            {"1r5k/P7/8/8/8/8/8/4K3 b - - 0 1", "h8h7"},
            {"4k3/8/8/8/8/8/p7/1R5K w - - 0 1", "h1h2"}}};
        for (bool classical : {false, true}) {
            const auto evaluator = model(parameters, classical);
            for (const auto& fixture : fixtures) {
                Board board(fixture.fen);
                const Board saved = board;
                const Move root_move = Move::from_uci(fixture.move);
                board.play(root_move);
                const int oracle = -independent_quiescence(board, parameters, classical);
                board.undo();
                for (bool fast : {false, true}) {
                    SearchOptions options;
                    options.neural = evaluator;
                    options.nnue = true;
                    options.quiescence_checks = false;
                    options.fast_moves = fast;
                    options.lmr = false;
                    options.root_moves = std::vector<Move>{root_move};
                    SearchLimits limits;
                    limits.depth = 1;
                    const auto result = search(board, limits, nullptr, {}, options);
                    expect(result.score == oracle && result.depth == 1 && !result.stopped && result.qnodes > 0,
                           "qsearch differs from independent oracle after " + fixture.move);
                    expect(board == saved, "qsearch special move changed input board/history");
                }
            }
        }
    });
    test("cancelled fallback distinguishes quantized mode and honors evaluator precedence", [] {
        auto parameters = zero();
        parameters[12] = 0.4 / static_cast<double>(scale);
        parameters[w2] = 100;
        parameters[w3] = 100;
        parameters[b3] = 0.49 / 600.0;
        const auto evaluator = model(parameters);
        const Board board;
        const Board saved = board;
        expect(evaluator->evaluate(board) == 1 && reference_score(parameters, false, board) == 0,
               "rounding fixture must distinguish original and quantized inference");
        const std::atomic_bool stop{true};
        SearchLimits limits;
        limits.depth = 3;
        SearchContext context;
        for (bool quantized : {false, true, false, true}) {
            SearchOptions options;
            options.neural = evaluator;
            options.nnue = quantized;
            const auto result = search(board, limits, &stop, {}, options, &context);
            expect(result.score == (quantized ? 0 : 1) && result.depth == 0 && result.stopped,
                   "cancelled fallback ignored selected neural arithmetic");
            expect(result.best_move.has_value(), "cancelled fallback has no legal move");
            options.positional = false;
            expect(search(board, limits, &stop, {}, options, &context).score == evaluate_material(board),
                   "material-only selection failed to override neural mode");
            options.positional = true;
            options.neural.reset();
            if (quantized) {
                bool rejected = false;
                try { (void)search(board, limits, &stop, {}, options, &context); }
                catch (const std::invalid_argument&) { rejected = true; }
                expect(rejected, "NNUE flag without a model was accepted");
            } else {
                expect(search(board, limits, &stop, {}, options, &context).score == evaluate_position(board),
                       "absent model changed classical fallback");
            }
        }
        expect(board == saved, "cancelled search changed board/history");
    });
    test("shared search memory remains safe across models, modes, options and cancellation", [] {
        const auto first = model(sampled(2026), true);
        const auto second = model(sampled(1019), false);
        SearchContext context;
        const std::atomic_bool stop{true};
        Board board;
        for (const char* move : {"g1f3", "g8f6", "f3g1", "f6g8"}) board.play(move);
        const Board saved = board;
        SearchLimits limits;
        limits.depth = 3;
        limits.nodes = 2500;
        for (int index = 0; index < 8; ++index) {
            SearchOptions options;
            options.neural = index % 2 == 0 ? first : second;
            options.nnue = index % 3 != 0;
            options.lmr = index % 2 == 0;
            options.fast_moves = index % 2 != 0;
            options.pawn_structure = index % 3 != 1;
            options.king_pawn_shield = index % 3 != 2;
            const auto* cancelled = index == 2 ? &stop : nullptr;
            const auto fresh = search(board, limits, cancelled, {}, options);
            const auto reused = search(board, limits, cancelled, {}, options, &context);
            same_search(fresh, reused);
            expect(board == saved, "model/mode change leaked board state");
        }
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
