#include "regicide/neural.hpp"

#include <array>
#include <functional>
#include <iomanip>
#include <iostream>
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
std::shared_ptr<const NeuralEvaluator> model(const Parameters& parameters) {
    std::istringstream input(serialized(parameters));
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
} // namespace

int main() {
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
