#include "regicide/neural.hpp"
#include "regicide/evaluation.hpp"
#include "reference_neural_v063.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <functional>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
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
using Parameters = std::vector<double>;
Parameters zero() { return Parameters(NeuralEvaluator::parameter_count, 0.0); }
std::string serialized(const Parameters& parameters,
                       const std::string& header = "REGICIDE_NN_V1 768 32 16 600") {
    std::ostringstream out;
    out << header << '\n' << std::setprecision(17);
    for (double parameter : parameters) out << parameter << '\n';
    return out.str();
}
std::shared_ptr<const NeuralEvaluator> model(const Parameters& parameters,
                                           const std::string& header = "REGICIDE_NN_V1 768 32 16 600") {
    std::istringstream input(serialized(parameters, header));
    return NeuralEvaluator::load(input);
}
void rejects(const std::string& text) {
    bool rejected = false;
    try {
        std::istringstream input(text);
        (void)NeuralEvaluator::load(input);
    } catch (const std::runtime_error&) { rejected = true; }
    expect(rejected, "invalid model was accepted");
}
Parameters feature_path(std::size_t feature, double weight = 1.0) {
    auto parameters = zero();
    parameters[feature] = weight;
    parameters[w2] = 1;
    parameters[w3] = 1;
    return parameters;
}

std::vector<Board> parity_positions() {
    // Special rules, sparse/dense inputs, both orientations and histories are
    // represented independently of whichever positions a benchmark selects.
    const std::array<const char*, 12> seeds{{
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
        "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1",
        "r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1",
        "4k3/P7/8/8/8/8/7p/4K3 w - - 0 1",
        "7k/8/8/BB6/NN6/RR6/Q1QQ4/KQQQQQQ1 w - - 0 1",
        "7k/6Q1/5K2/8/8/8/8/8 b - - 0 1",
        "7k/5Q2/5K2/8/8/8/8/8 b - - 0 1",
        "8/8/8/8/2k5/8/5K2/8 b - - 99 50",
        "4k3/8/3p4/2pPp3/2P1P3/8/8/4K3 b - - 0 1",
        "3k4/8/8/2r5/8/4R3/8/4K3 w - - 74 60",
        "8/1b3pk1/2p2np1/p3p3/P3P3/2P2NP1/1B3PK1/8 b - - 0 20"}};
    std::vector<Board> boards;
    boards.reserve(1025);
    for (const char* fen : seeds) boards.emplace_back(fen);
    Board repeated;
    for (const char* move : {"g1f3", "g8f6", "f3g1", "f6g8",
                             "g1f3", "g8f6", "f3g1", "f6g8"}) repeated.play(move);
    boards.push_back(repeated);
    std::mt19937 rng(0x063064U);
    Board board;
    while (boards.size() < 1025) {
        const auto moves = board.legal_moves();
        if (moves.empty() || board.outcome() != Outcome::ongoing || board.history_size() >= 160)
            board = Board(seeds[rng() % seeds.size()]);
        else board.play_generated(moves[rng() % moves.size()]);
        boards.push_back(board);
    }
    return boards;
}

void expect_parity(const Parameters& parameters, bool classical,
                   const std::vector<Board>& boards, const std::string& label) {
    const auto evaluator = model(parameters, classical ? "REGICIDE_NN_V2 768 32 16 600 100" :
                                                       "REGICIDE_NN_V1 768 32 16 600");
    const reference_v063::NeuralEvaluator reference(parameters, classical);
    for (std::size_t index = 0; index < boards.size(); ++index) {
        const Board saved = boards[index];
        for (bool shield : {false, true}) {
            for (bool structure : {false, true}) {
                const int expected = reference.evaluate(boards[index], shield, structure);
                expect(evaluator->evaluate(boards[index], shield, structure) == expected,
                       label + " differs from frozen v0.6.3 at board " + std::to_string(index));
            }
        }
        expect(boards[index] == saved, label + " changed full board or history");
        if (saved.history_size() > 0) {
            Board after_evaluation = boards[index];
            Board expected_undo = saved;
            after_evaluation.undo();
            expected_undo.undo();
            expect(after_evaluation == expected_undo, label + " changed subsequent undo");
        }
    }
}

std::filesystem::path source_fixture(const char* relative) {
    const auto from_source = std::filesystem::path(__FILE__).parent_path().parent_path() / relative;
    if (std::filesystem::is_regular_file(from_source)) return from_source;
    for (auto root = std::filesystem::current_path(); !root.empty();) {
        const auto candidate = root / relative;
        if (std::filesystem::is_regular_file(candidate)) return candidate;
        const auto parent = root.parent_path();
        if (parent == root) break;
        root = parent;
    }
    throw std::runtime_error("Cannot locate committed neural parity fixture: " + std::string(relative));
}
} // namespace

int main() {
    test("frozen v0.6.3 parity over seeded models both formats and legal histories", [] {
        const auto boards = parity_positions();
        std::mt19937 rng(2026063064);
        std::uniform_real_distribution<double> random(-1.0, 1.0);
        for (double first_layer_scale : {0.0, 0.001, 0.03, 0.3, 3.0}) {
            auto parameters = zero();
            for (std::size_t index = 0; index < b1; ++index)
                parameters[index] = random(rng) * first_layer_scale;
            for (std::size_t index = b1; index < w2; ++index)
                parameters[index] = random(rng) * first_layer_scale;
            for (std::size_t index = w2; index < b3; ++index)
                parameters[index] = random(rng) * 0.1;
            parameters[b3] = random(rng) * 0.001;
            for (bool classical : {false, true})
                expect_parity(parameters, classical, boards, "seeded model");
        }
    });
    test("committed learned V2 models match frozen inference over legal histories", [] {
        const auto boards = parity_positions();
        for (const char* fixture : {
                 "docs/checks/2026-10-19-neural-cost-quality/diagnostic/learned-v2.nn",
                 "docs/checks/2026-10-19-neural-cost-quality/run/model/small-v2.nn"}) {
            const auto path = source_fixture(fixture).string();
            const auto evaluator = NeuralEvaluator::load(path);
            const auto reference = reference_v063::NeuralEvaluator::load(path);
            for (std::size_t index = 0; index < boards.size(); ++index) {
                const Board saved = boards[index];
                for (bool shield : {false, true}) {
                    for (bool structure : {false, true})
                        expect(evaluator->evaluate(boards[index], shield, structure) ==
                                   reference->evaluate(boards[index], shield, structure),
                               "learned model differs from frozen inference at board " + std::to_string(index));
                }
                expect(boards[index] == saved, "learned model mutated board or history");
            }
        }
    });
    test("cancellation and half-centipawn boundaries retain original accumulation order", [] {
        const Board board;
        const double epsilon = std::ldexp(1.0, -47);
        const double below_half = std::nextafter(1.0 / 1200.0, 0.0);
        for (int layer : {1, 2}) {
            for (double sign : {-1.0, 1.0}) {
                auto parameters = zero();
                if (layer == 1) {
                    parameters[3 * 64] = 100; // a1 rook, first occupied square.
                    parameters[64 + 1] = -100; // b1 knight, second square.
                    parameters[2 * 64 + 2] = epsilon; // c1 bishop follows cancellation.
                    parameters[w2] = 100;
                    parameters[w3] = sign * 100;
                } else {
                    parameters[b1] = 100;
                    parameters[b1 + 1] = 100;
                    parameters[b1 + 2] = epsilon;
                    parameters[w2] = 1;
                    parameters[w2 + 1] = -1;
                    parameters[w2 + 2] = 1;
                    parameters[w3] = sign * 100;
                }
                parameters[b3] = sign * below_half;
                const reference_v063::NeuralEvaluator reference(parameters, false);
                // Regrouping the three terms can erase epsilon and round to 0.
                expect(reference.evaluate(board) == static_cast<int>(sign),
                       "cancellation fixture no longer distinguishes half rounding");
                for (double bias : {std::nextafter(parameters[b3], -1.0), parameters[b3],
                                    std::nextafter(parameters[b3], 1.0)}) {
                    parameters[b3] = bias;
                    expect_parity(parameters, false, {board}, "cancellation boundary");
                    expect_parity(parameters, true, {board}, "bounded cancellation boundary");
                }
            }
        }
    });
    test("separate multiply and add semantics remain visible at rounding boundaries", [] {
        auto parameters = zero();
        parameters[b1] = 1.0 + std::ldexp(1.0, -27);
        parameters[w2] = -(1.0 - std::ldexp(1.0, -27));
        parameters[b2] = 1;
        parameters[w3] = 100;
        parameters[b3] = std::nextafter(1.0 / 1200.0, 0.0);
        const reference_v063::NeuralEvaluator reference(parameters, false);
        expect(reference.evaluate(Board()) == 0, "frozen scalar multiply/add fixture changed");
        expect_parity(parameters, false, {Board()}, "multiply/add boundary");
        expect_parity(parameters, true, {Board()}, "bounded multiply/add boundary");
        for (double tiny : {std::ldexp(1.0, -500), std::ldexp(1.0, -1022), 0.0, -0.0}) {
            parameters[b1] = tiny;
            parameters[b2] = -tiny;
            expect_parity(parameters, false, {Board()}, "tiny/signed-zero activation");
            expect_parity(parameters, true, {Board()}, "bounded tiny/signed-zero activation");
        }
    });
    test("V2 zero correction preserves every classical option and board state", [] {
        const auto evaluator = model(zero(), "REGICIDE_NN_V2 768 32 16 600 100");
        std::mt19937 rng(20261019);
        Board board;
        for (int index = 0; index < 240; ++index) {
            const Board saved = board;
            for (bool shield : {false, true}) {
                for (bool structure : {false, true}) {
                    const int expected = evaluate_position(board, shield, structure);
                    expect(evaluator->evaluate(board, shield, structure) == expected,
                           "zero V2 lost an existing positional component");
                }
            }
            expect(board == saved, "V2 evaluation changed board state or draw history");
            const auto moves = board.legal_moves();
            if (moves.empty() || index % 80 == 79) board = Board();
            else board.play(moves[rng() % moves.size()]);
        }
    });
    test("V2 correction is bounded and uses classical position rather than material", [] {
        const Board board("4k3/1P6/8/8/8/8/8/4K3 w - - 0 1");
        const int baseline = evaluate_position(board);
        expect(baseline != 100, "passed pawn fixture lacks positional value");
        for (double raw : {-100.0, -1.0, 0.0, 1.0, 100.0}) {
            auto parameters = zero();
            parameters[b3] = raw;
            const int score = model(parameters, "REGICIDE_NN_V2 768 32 16 600 100")->evaluate(board);
            const int expected = static_cast<int>(std::lround(baseline + 100.0 * std::tanh(raw)));
            expect(score == expected && std::abs(score - baseline) <= 100,
                   "V2 correction exceeded its bound or dropped the classical baseline");
        }
    });
    test("V2 feature paths have the same orientation as V1", [] {
        const auto evaluator = model(feature_path(10, 0.25), "REGICIDE_NN_V2 768 32 16 600 100");
        const Board white("4k3/8/8/8/8/8/2P5/4K3 w - - 0 1");
        const Board black("4k3/2p5/8/8/8/8/8/4K3 b - - 0 1");
        const int correction = static_cast<int>(std::lround(100.0 * std::tanh(0.25)));
        expect(evaluator->evaluate(white) == evaluate_position(white) + correction,
               "V2 own feature orientation mismatch");
        expect(evaluator->evaluate(black) == evaluator->evaluate(white),
               "V2 color/rank reflection mismatch");
    });
    test("V2 retains bounded corrections in legal promotion-heavy positions", [] {
        const Board board("7k/8/8/BB6/NN6/RR6/Q1QQ4/KQQQQQQ1 w - - 0 1");
        const int baseline = evaluate_position(board);
        expect(baseline > 10000, "promotion fixture must exceed the legacy clamp");
        for (double raw : {-100.0, 0.0, 100.0}) {
            auto parameters = zero();
            parameters[b3] = raw;
            const int expected = baseline + static_cast<int>(std::lround(100.0 * std::tanh(raw)));
            expect(model(parameters, "REGICIDE_NN_V2 768 32 16 600 100")->evaluate(board) == expected,
                   "legacy clamp removed classical promotion value from V2");
        }
    });
    test("zero residual preserves exact side-to-move material", [] {
        const auto evaluator = model(zero());
        for (const auto& [fen, expected] : std::array<std::pair<const char*, int>, 5>{{
                 {"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 0},
                 {"4k3/8/8/8/8/8/PP6/4K3 w - - 0 1", 200},
                 {"4k3/8/8/8/8/8/PP6/4K3 b - - 0 1", -200},
                 {"4k3/8/8/8/8/8/4n3/Q2RK3 w - - 0 1", 1080},
                 {"4k3/8/8/8/8/8/8/4K3 b - - 0 1", 0}}})
            expect(evaluator->evaluate(Board(fen)) == expected, "incorrect material convention");
    });
    test("hand-computed affine layers use output-major weights", [] {
        auto parameters = feature_path(10, 0.25); // Own pawn c2.
        parameters[b1] = 0.5;
        parameters[w2] = 2;
        parameters[b2] = -0.25;
        parameters[w3] = 0.5;
        parameters[b3] = -0.125;
        const auto evaluator = model(parameters);
        // ((0.25 + 0.5) * 2 - 0.25) * 0.5 - 0.125 = 0.5.
        expect(evaluator->evaluate(Board("4k3/8/8/8/8/8/2P5/4K3 w - - 0 1")) == 400,
               "three-layer hand-computed output mismatch");
        expect(evaluator->evaluate(Board("4k3/2p5/8/8/8/8/8/4K3 b - - 0 1")) == 400,
               "black side-to-move features must rank-reflect and swap color channels");
    });
    test("own and opposing pawn channels remain distinct", [] {
        const auto evaluator = model(feature_path(10));
        expect(evaluator->evaluate(Board("4k3/8/8/8/8/8/2P5/4K3 w - - 0 1")) == 700,
               "own pawn c2 feature not active");
        expect(evaluator->evaluate(Board("4k3/2p5/8/8/8/8/8/4K3 w - - 0 1")) == -100,
               "opposing pawn incorrectly used own feature channel");
        const auto opposing = model(feature_path(6 * 64 + 50)); // Opponent pawn c7.
        expect(opposing->evaluate(Board("4k3/2p5/8/8/8/8/8/4K3 w - - 0 1")) == 500,
               "opponent channel or square orientation is incorrect");
        expect(opposing->evaluate(Board("4k3/8/8/8/8/8/2P5/4K3 b - - 0 1")) == 500,
               "black opponent channels must rank-reflect, not rotate files");
    });
    test("all six piece types use PNBRQK channel order", [] {
        const std::array<const char*, 6> positions{{
            "4k3/8/8/8/3P4/8/8/4K3 w - - 0 1",
            "4k3/8/8/8/3N4/8/8/4K3 w - - 0 1",
            "4k3/8/8/8/3B4/8/8/4K3 w - - 0 1",
            "4k3/8/8/8/3R4/8/8/4K3 w - - 0 1",
            "4k3/8/8/8/3Q4/8/8/4K3 w - - 0 1",
            "4k3/8/8/8/3K4/8/8/8 w - - 0 1"}};
        const std::array<int, 6> material{{100, 320, 330, 500, 900, 0}};
        for (std::size_t type = 0; type < positions.size(); ++type) {
            const auto evaluator = model(feature_path(type * 64 + 27, 0.5));
            expect(evaluator->evaluate(Board(positions[type])) == material[type] + 300,
                   "PNBRQK channel order mismatch");
        }
    });
    test("later rows and columns are not transposed or omitted", [] {
        auto parameters = zero();
        parameters[31 * 768 + 10] = 0.5;
        parameters[b1 + 31] = 0.25;
        parameters[w2 + 15 * 32 + 31] = 2;
        parameters[b2 + 15] = 0.5;
        parameters[w3 + 15] = 0.25;
        expect(model(parameters)->evaluate(Board("4k3/8/8/8/8/8/2P5/4K3 w - - 0 1")) == 400,
               "final hidden rows must contribute to the scalar output");
    });
    test("ReLU is applied independently to both hidden layers", [] {
        const Board board;
        auto parameters = zero();
        parameters[b1] = -2;
        parameters[w2] = -3;
        parameters[w3] = 1;
        expect(model(parameters)->evaluate(board) == 0, "negative first hidden unit passed ReLU");
        parameters[b1] = 1;
        expect(model(parameters)->evaluate(board) == 0, "negative second hidden unit passed ReLU");
        parameters[b2] = 4;
        expect(model(parameters)->evaluate(board) == 600, "positive hidden activation was lost");
    });
    test("rounding halves go away from zero", [] {
        auto parameters = zero();
        parameters[b3] = 1.0 / 1200.0;
        expect(model(parameters)->evaluate(Board()) == 1, "positive half centipawn must round up");
        parameters[b3] = -1.0 / 1200.0;
        expect(model(parameters)->evaluate(Board()) == -1, "negative half centipawn must round down");
        parameters[b3] = 0.49 / 600.0;
        expect(model(parameters)->evaluate(Board()) == 0, "sub-half residual must round to zero");
    });
    test("output clamps protect the search mate-score range", [] {
        auto parameters = zero();
        parameters[b3] = 100;
        expect(model(parameters)->evaluate(Board()) == 10000, "positive output clamp mismatch");
        parameters[b3] = -100;
        expect(model(parameters)->evaluate(Board()) == -10000, "negative output clamp mismatch");
    });
    test("evaluation preserves position history draw state and undo", [] {
        Board board;
        for (const char* move : {"g1f3", "g8f6", "f3g1", "f6g8",
                                 "g1f3", "g8f6", "f3g1", "f6g8"}) board.play(move);
        const Board saved = board;
        const auto evaluator = model(feature_path(4));
        for (int repeat = 0; repeat < 10; ++repeat) (void)evaluator->evaluate(board);
        expect(board == saved && board.claims_now().threefold,
               "neural evaluation changed full board or repetition claims");
        board.undo();
        Board expected = saved;
        expected.undo();
        expect(board == expected, "neural evaluation affected later undo behavior");
    });
    test("terminal adjudication is deliberately outside the evaluator", [] {
        auto parameters = zero();
        parameters[b3] = 1;
        const Board board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1");
        expect(board.outcome() == Outcome::checkmate, "mate fixture is not checkmate");
        expect(model(parameters)->evaluate(board) == -300,
               "neural evaluator must return a static score without overriding search adjudication");
    });
    test("strict header rejects versions dimensions scale and extra fields", [] {
        for (const char* header : {"REGICIDE_NN_V2 768 32 16 600", "REGICIDE_NN_V1 767 32 16 600",
                                  "REGICIDE_NN_V1 768 31 16 600", "REGICIDE_NN_V1 768 32 15 600",
                                  "REGICIDE_NN_V1 768 32 16 601", "REGICIDE_NN_V1 768 32 16 600 extra",
                                  "REGICIDE_NN_V1 768 32 16", "REGICIDE_NN_V1 768.0 32 16 600"})
            rejects(serialized(zero(), header));
        rejects("");
        for (const char* header : {"REGICIDE_NN_V3 768 32 16 600 100",
                                  "REGICIDE_NN_V2 768 32 16 600 101",
                                  "REGICIDE_NN_V2 768 32 16 600 100.0",
                                  "REGICIDE_NN_V2 768 32 16 600 100 extra",
                                  "REGICIDE_NN_V1 768 32 16 600 100"})
            rejects(serialized(zero(), header));
    });
    test("parameter counts and trailing tokens must match exactly", [] {
        auto parameters = zero();
        parameters.pop_back();
        rejects(serialized(parameters));
        parameters.push_back(0);
        rejects(serialized(parameters) + "0\n");
        rejects(serialized(parameters) + "junk\n");
        std::string with_nul = serialized(parameters);
        with_nul.push_back('\0');
        rejects(with_nul);
        rejects("REGICIDE_NN_V1 768 32 16 600\n0\n");
    });
    test("non-finite excessive and malformed weights are rejected", [] {
        for (const char* bad : {"nan", "NaN", "inf", "-inf", "1e9999", "100.0001", "-100.0001", "no"}) {
            auto text = serialized(zero());
            const auto start = text.find('\n') + 1;
            text.replace(start, 1, bad);
            rejects(text);
        }
        auto text = serialized(zero());
        text.replace(text.size() - 2, 1, "0x1p0");
        rejects(text);
    });
    test("bounded loading rejects oversized inputs and nonexistent files", [] {
        rejects(std::string(NeuralEvaluator::max_file_bytes + 1, ' '));
        bool rejected = false;
        try { (void)NeuralEvaluator::load("__regicide_missing_neural_model_test__.nn"); }
        catch (const std::runtime_error&) { rejected = true; }
        expect(rejected, "loading a missing file must fail explicitly");
    });
    test("format accepts CRLF and trailing whitespace", [] {
        std::string text = serialized(zero());
        const auto first_line = text.find('\n');
        text.insert(first_line, "\r");
        text += " \t\r\n";
        std::istringstream input(text);
        expect(NeuralEvaluator::load(input)->evaluate(Board()) == 0,
               "text whitespace changed a valid model");
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
