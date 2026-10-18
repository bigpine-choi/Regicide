#include "regicide/board.hpp"
#include "regicide/evaluation.hpp"
#include "regicide/search.hpp"

#include <exception>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>

// Diagnostic driver only: no search, engine option changes, or evaluator changes.
// Input is initial_fen<TAB>space-separated full UCI history, one case per line.
// Exactly one TAB is required; an empty history after it is valid.
// Output has no header. The fixed TSV schema is:
// canonical_fen, side_to_move, material_cp,
// eval_shield_on_structure_on_cp, eval_shield_on_structure_off_cp,
// eval_shield_off_structure_on_cp, eval_shield_off_structure_off_cp,
// legal_count, in_check, claim_threefold_now, claim_fifty_moves_now,
// outcome, repetitions.
// Side is w/b; booleans are 0/1. Every score favors the side to move.
// These are static observations of supplied positions, not searched-leaf values.
// Static evaluation does not replace terminal outcomes or draw-claim decisions.
// Differences between the four configurations are NOT an additive feature
// decomposition: integer rounding in phase blending can prevent additivity.
namespace {
void help() {
    std::cout
        << "Regicide static evaluation diagnostic probe\n"
           "Usage: regicide_evaluation_probe [--help]\n"
           "stdin: initial six-field FEN<TAB>space-separated full UCI history\n"
           "Exactly one TAB is required. Empty history is allowed.\n"
           "Replays the full history with legal-move validation; earlier history\n"
           "before the supplied initial FEN remains unknown.\n"
           "stdout: one TSV row per input row, without a header:\n"
           "canonical_fen\tside_to_move\tmaterial_cp\t"
           "eval_shield_on_structure_on_cp\teval_shield_on_structure_off_cp\t"
           "eval_shield_off_structure_on_cp\teval_shield_off_structure_off_cp\t"
           "legal_count\tin_check\tclaim_threefold_now\tclaim_fifty_moves_now\t"
           "outcome\trepetitions\n"
           "Side is w/b; booleans are 0/1. Scores favor the side to move.\n"
           "Static scores do not incorporate terminal or draw-claim decisions.\n"
           "Configuration differences are not an additive feature decomposition.\n"
           "The supplied positions are not asserted to be actual searched leaves.\n"
           "On invalid input: stderr reports the line number; exit 1 immediately.\n"
           "Any earlier valid output rows are preserved.\n";
}

void inspect(std::string line) {
    // Accept redirected CRLF text on platforms whose streams do not translate it.
    if (!line.empty() && line.back() == '\r') line.pop_back();
    const auto split = line.find('\t');
    if (split == std::string::npos || line.find('\t', split + 1) != std::string::npos)
        throw std::invalid_argument("expected exactly one TAB between FEN and UCI history");
    if (line.find_first_of("\r\v\f") != std::string::npos ||
        line.find('\0') != std::string::npos)
        throw std::invalid_argument("unexpected control character in input");

    regicide::Board board(line.substr(0, split));
    std::istringstream history(line.substr(split + 1));
    std::string move;
    while (history >> move) board.play(move);

    const auto claims = board.claims_now();
    const auto legal_count = board.legal_moves().size();
    const auto outcome = board.outcome();
    std::cout << board.fen() << '\t'
              << (board.position().turn == regicide::Color::white ? 'w' : 'b') << '\t'
              << regicide::evaluate_material(board) << '\t'
              << regicide::evaluate_position(board, true, true) << '\t'
              << regicide::evaluate_position(board, true, false) << '\t'
              << regicide::evaluate_position(board, false, true) << '\t'
              << regicide::evaluate_position(board, false, false) << '\t'
              << legal_count << '\t' << board.in_check() << '\t'
              << claims.threefold << '\t' << claims.fifty_moves << '\t'
              << regicide::name(outcome) << '\t' << board.repetition_count() << '\n';
}
} // namespace

int main(int argc, char** argv) {
    if (argc == 2 && std::string_view(argv[1]) == "--help") {
        help();
        return std::cout ? 0 : 1;
    }
    if (argc != 1) {
        std::cerr << "error: unexpected arguments; see --help\n";
        return 1;
    }

    std::string line;
    std::size_t line_number = 0;
    while (std::getline(std::cin, line)) {
        ++line_number;
        try {
            inspect(line);
            if (!std::cout) throw std::runtime_error("cannot write diagnostic output");
        } catch (const std::exception& error) {
            std::cerr << "line " << line_number << ": " << error.what() << '\n';
            return 1;
        }
    }
    if (std::cin.bad()) {
        std::cerr << "line " << line_number + 1 << ": cannot read diagnostic input\n";
        return 1;
    }
    return 0;
}
