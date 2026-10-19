#include "regicide/evaluation.hpp"
#include "regicide/search.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <functional>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>

using namespace regicide;
namespace {
int assertions = 0, groups = 0, failures = 0;
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

std::string fen(const Position& position) {
    std::ostringstream out;
    for (int rank = 7; rank >= 0; --rank) {
        int empty = 0;
        for (int file = 0; file < 8; ++file) {
            const char piece = position.squares[rank * 8 + file];
            if (piece == '.') { ++empty; continue; }
            if (empty) out << empty;
            empty = 0;
            out << piece;
        }
        if (empty) out << empty;
        if (rank) out << '/';
    }
    out << (position.turn == Color::white ? " w " : " b ");
    if (!position.castling) out << '-';
    for (const auto& [mask, symbol] : std::array<std::pair<unsigned, char>, 4>{
             {{1, 'K'}, {2, 'Q'}, {4, 'k'}, {8, 'q'}}})
        if (position.castling & mask) out << symbol;
    out << ' ';
    if (position.en_passant < 0) out << '-';
    else out << static_cast<char>('a' + position.en_passant % 8)
             << static_cast<char>('1' + position.en_passant / 8);
    out << ' ' << position.halfmove << ' ' << position.fullmove;
    return out.str();
}

Board reflected(const Board& board, bool swap_turn) {
    Position result = board.position();
    for (int square = 0; square < 64; ++square) {
        const char piece = board.position().squares[square];
        const char swapped = piece == '.' ? '.' :
            static_cast<char>(std::isupper(static_cast<unsigned char>(piece))
                ? std::tolower(static_cast<unsigned char>(piece))
                : std::toupper(static_cast<unsigned char>(piece)));
        result.squares[square ^ 56] = swapped;
    }
    result.castling = ((result.castling & 3) << 2) | ((result.castling & 12) >> 2);
    if (result.en_passant >= 0) result.en_passant ^= 56;
    if (swap_turn) result.turn = opposite(result.turn);
    return Board(fen(result));
}

void symmetric(const Board& board) {
    const auto saved = board;
    const int score = evaluate_neural(board);
    expect(evaluate_neural(reflected(board, true)) == score,
           "color/rank reflection with swapped turn must preserve relative score");
    // Changing only the active color isolates the sign convention when both
    // kings are safe (the FEN reader rejects a checked side that just moved).
    if (!board.in_check()) {
        Position flipped = board.position();
        flipped.turn = opposite(flipped.turn);
        // En passant is tied to the next mover; it does not affect evaluation.
        flipped.en_passant = -1;
        expect(evaluate_neural(Board(fen(flipped))) == -score,
               "changing turn must negate the evaluation exactly");
    }
    expect(board == saved, "evaluation changed the board or history");
}
} // namespace

int main() {
 test("perspective, determinism, history and undo", [] {
  Board board;
  expect(evaluate_neural(board) == 0, "initial symmetry");
  for (int ply = 0; ply < 100; ++ply) {
   symmetric(board);
   expect(evaluate_neural(board) == evaluate_neural(Board(board.fen())), "fresh FEN");
   auto moves = board.legal_moves(); if (moves.empty()) break;
   const auto saved = board; const int score = evaluate_neural(board);
   const auto move = moves[static_cast<std::size_t>((ply * 37 + 11) % moves.size())];
   board.play(move); board.undo();
   expect(board == saved && evaluate_neural(board) == score, "undo"); board.play(move);
  }
 });
 test("special moves", [] {
  for (const auto& [source, move] : std::array<std::pair<const char*, const char*>, 8>{{
   {"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"},
   {"r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8"},
   {"7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1", "e5d6"},
   {"7k/8/8/8/3pP3/8/8/6K1 b - e3 0 1", "d4e3"},
   {"7k/P7/8/8/8/8/8/6K1 w - - 0 1", "a7a8q"},
   {"7k/P7/8/8/8/8/8/6K1 w - - 0 1", "a7a8n"},
   {"1r5k/P7/8/8/8/8/8/6K1 w - - 0 1", "a7b8b"},
   {"7k/8/8/8/8/8/p7/6K1 b - - 0 1", "a2a1r"}
  }}) {
   Board board(source); const auto saved = board; const int score = evaluate_neural(board);
   board.play(move); symmetric(board);
   expect(evaluate_neural(board) == evaluate_neural(Board(board.fen())), "special refresh");
   board.undo(); expect(board == saved && evaluate_neural(board) == score, "special undo");
  }
 });
 test("search selection, fallback, terminal and context reuse", [] {
  Board board("7k/8/8/8/8/8/P7/6K1 w - - 0 1"); SearchLimits limits; limits.depth = 2;
  SearchOptions options; options.neural = true; SearchContext context;
  const auto result = search(board, limits, nullptr, {}, options, &context);
  expect(result.best_move.has_value() && result.depth == 2, "neural search");
  options.positional = false;
  auto override_result = search(board, limits, nullptr, {}, options, &context);
  expect(result.score == override_result.score && result.pv == override_result.pv, "precedence");
  std::atomic_bool stop{true};
  expect(search(board, limits, &stop, {}, options).score == evaluate_neural(board), "fallback");
  options.neural = false; options.positional = true;
  auto reused = search(board, limits, nullptr, {}, options, &context); auto fresh = search(board, limits);
  expect(reused.score == fresh.score && reused.pv == fresh.pv, "cache isolation");
  options.neural = true;
  expect(search(Board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1"), limits, nullptr, {}, options).score == -mate_score, "mate");
 });
 std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
 return failures ? 1 : 0;
}
