#include "regicide/evaluation.hpp"
#include "regicide/nnue.hpp"
#include "regicide/neural.hpp"
#include "regicide/search.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdint>
#include <functional>
#include <iostream>
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

void exact_accumulator(const Board& board, const NeuralAccumulator& accumulator,
                       const std::string& label) {
    const auto saved = board;
    expect(accumulator == NeuralAccumulator(board), label + ": raw sums differ from refresh");
    const int expected = evaluate_neural(board);
    expect(accumulator.evaluate(board.position().turn) == expected,
           label + ": incremental score differs from full refresh");
    expect(accumulator.evaluate(opposite(board.position().turn)) == -expected,
           label + ": opposite score perspective");
    expect(board == saved, label + ": evaluation changed board/history");
}

void apply(Board& board, NeuralAccumulator& accumulator, Move move, bool fast) {
    const Position before = board.position();
    if (fast) board.play_generated(move);
    else board.play(move);
    accumulator.update_generated(before, board.position(), move);
}

void siblings(Board& board, NeuralAccumulator& accumulator, bool fast,
              std::size_t maximum = 1000) {
    const auto saved_board = board;
    const auto saved_accumulator = accumulator;
    const auto moves = board.legal_moves();
    for (std::size_t i = 0; i < std::min(maximum, moves.size()); ++i) {
        apply(board, accumulator, moves[i], fast);
        exact_accumulator(board, accumulator, "sibling " + moves[i].uci());
        board.undo(); accumulator = saved_accumulator;
        expect(board == saved_board, "sibling undo changed exact board/history");
        exact_accumulator(board, accumulator, "sibling undo");
    }
}

// Compare every stable search field; memory allocation/reuse counters depend on
// context ownership, and elapsed time necessarily differs between evaluators.
void same_graph(const SearchResult& left, const SearchResult& right) {
    expect(left.best_move == right.best_move && left.score == right.score &&
               left.depth == right.depth && left.pv == right.pv && left.stopped == right.stopped,
           "incremental/full search result differs");
    expect(left.nodes == right.nodes && left.qnodes == right.qnodes &&
               left.seldepth == right.seldepth && left.tt_probes == right.tt_probes &&
               left.tt_hits == right.tt_hits && left.tt_cutoffs == right.tt_cutoffs &&
               left.tt_stores == right.tt_stores && left.pvs_probes == right.pvs_probes &&
               left.pvs_researches == right.pvs_researches && left.see_calls == right.see_calls &&
               left.tt_bytes == right.tt_bytes && left.lmr_reductions == right.lmr_reductions &&
               left.lmr_researches == right.lmr_researches,
           "incremental/full search graph differs");
}

std::uint32_t next_random(std::uint32_t& state) {
    state ^= state << 13; state ^= state >> 17; state ^= state << 5;
    return state;
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
 test("incremental special moves, sibling branches and exact undo", [] {
  std::vector<std::pair<std::string, std::string>> fixtures{
   {"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"},
   {"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1c1"},
   {"r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8g8"},
   {"r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8"},
   {"7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1", "e5d6"},
   {"7k/8/8/8/3pP3/8/8/6K1 b - e3 0 1", "d4e3"},
   {std::string(Board::start_fen), "e2e4"},
   {"r3k2r/1P6/8/8/8/8/8/R3K2R w KQkq - 0 1", "b7a8q"}
  };
  for (char promotion : std::string("qrbn")) {
   fixtures.emplace_back("7k/P7/8/8/8/8/8/6K1 w - - 0 1", "a7a8" + std::string(1, promotion));
   fixtures.emplace_back("1r5k/P7/8/8/8/8/8/6K1 w - - 0 1", "a7b8" + std::string(1, promotion));
   fixtures.emplace_back("7k/8/8/8/8/8/p7/6K1 b - - 0 1", "a2a1" + std::string(1, promotion));
   fixtures.emplace_back("7k/8/8/8/8/8/p7/1R4K1 b - - 0 1", "a2b1" + std::string(1, promotion));
  }
  for (const auto& [source, text] : fixtures) for (bool fast : {false, true}) {
   Board board(source); NeuralAccumulator accumulator(board);
   const auto saved_board = board; const auto saved_accumulator = accumulator;
   exact_accumulator(board, accumulator, "special root");
   const Move move = Move::from_uci(text); const auto legal = board.legal_moves();
   expect(std::find(legal.begin(), legal.end(), move) != legal.end(), "special fixture must be legal");
   apply(board, accumulator, move, fast);
   exact_accumulator(board, accumulator, "special " + text);
   siblings(board, accumulator, fast);
   board.undo(); accumulator = saved_accumulator;
   expect(board == saved_board, "special undo exact board/history");
   exact_accumulator(board, accumulator, "special undo " + text);
   siblings(board, accumulator, fast);
  }
 });
 test("incremental raw values survive nonlinear clipping crossings", [] {
  Board board("7k/8/8/8/8/8/P7/6K1 w - - 0 1"); NeuralAccumulator accumulator(board);
  std::vector<std::pair<Board, NeuralAccumulator>> parents;
  // bootstrap-v1 white raw channel 8 follows 1,0,6,3,-2,0,1. Storing a
  // clipped activation instead of the raw sum corrupts later updates/undo.
  for (const char* text : {"a2a4", "h8g8", "a4a5", "g8f8", "g1f2", "f8e8"}) {
   parents.emplace_back(board, accumulator);
   apply(board, accumulator, Move::from_uci(text), false);
   exact_accumulator(board, accumulator, "clipping crossing");
  }
  while (!parents.empty()) {
   board.undo(); accumulator = parents.back().second;
   expect(board == parents.back().first, "clipping undo history");
   exact_accumulator(board, accumulator, "clipping undo"); parents.pop_back();
  }
 });
 test("seeded deep legal walks, sibling isolation and complete unwind", [] {
  const std::array<const char*, 3> roots{{
   "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
   "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
   "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1"}};
  for (const char* source : roots) for (std::uint32_t seed : {0x52454731U, 0x4e4e5545U}) {
   Board board(source); NeuralAccumulator accumulator(board);
   const auto root = board; const auto root_accumulator = accumulator;
   std::vector<std::pair<Board, NeuralAccumulator>> parents;
   int forward = 0;
   for (int ply = 0; ply < 160; ++ply) {
    exact_accumulator(board, accumulator, "walk root/parent");
    const auto moves = board.legal_moves(); if (moves.empty()) break;
    siblings(board, accumulator, (ply % 2) != 0, ply % 16 == 0 ? moves.size() : 2);
    const Move move = moves[next_random(seed) % moves.size()];
    parents.emplace_back(board, accumulator);
    apply(board, accumulator, move, (ply % 2) == 0); ++forward;
    exact_accumulator(board, accumulator, "walk child");
   }
   expect(forward >= 32, "walk fixture terminated before exercising deep state");
   while (!parents.empty()) {
    board.undo(); accumulator = parents.back().second;
    expect(board == parents.back().first, "deep undo exact board/history");
    exact_accumulator(board, accumulator, "deep undo"); parents.pop_back();
   }
   expect(board == root && accumulator == root_accumulator, "complete unwind did not restore root");
  }
 });
 test("incremental/full search parity across search options", [] {
  const std::array<const char*, 4> sources{{
   "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
   "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
   "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
   "1r5k/P7/8/8/8/8/8/6K1 w - - 0 1"}};
  SearchLimits limits; limits.depth = 2;
  for (bool quiet : {false, true}) for (bool pvs : {false, true})
   for (bool fast : {false, true}) for (const char* source : sources) {
    Board board(source); const auto saved = board; SearchContext context;
    SearchOptions options; options.nnue = true; options.hash_mb = 1;
    options.quiescence = quiet; options.pvs = pvs; options.fast_moves = fast;
    std::vector<SearchResult> incremental_iterations, full_iterations;
    const auto incremental = search(board, limits, nullptr,
      [&](const auto& result) { incremental_iterations.push_back(result); }, options, &context);
    options.nnue_incremental = false;
    const auto full = search(board, limits, nullptr,
      [&](const auto& result) { full_iterations.push_back(result); }, options, &context);
    same_graph(incremental, full);
    expect(incremental_iterations.size() == full_iterations.size(), "iteration count differs");
    for (std::size_t i = 0; i < incremental_iterations.size(); ++i)
     same_graph(incremental_iterations[i], full_iterations[i]);
    options.nnue_incremental = true;
    same_graph(incremental, search(board, limits, nullptr, {}, options, &context));
    expect(board == saved, "search changed input board/history");
   }
 });
 test("incremental/full cancellation, draw claims and context reuse", [] {
  Board board;
  for (const char* text : {"g1f3", "g8f6", "f3g1", "f6g8"}) board.play(text);
  const auto saved = board; SearchContext context; SearchOptions options;
  options.nnue = true; options.hash_mb = 1; SearchLimits limits; limits.depth = 3;
  for (bool fast : {false, true}) {
   limits.depth = 3;
   options.fast_moves = fast;
   for (std::uint64_t budget : {1ULL, 41ULL, 257ULL}) {
    limits.nodes = budget; options.nnue_incremental = true;
    const auto incremental = search(board, limits, nullptr, {}, options, &context);
    options.nnue_incremental = false;
    const auto full = search(board, limits, nullptr, {}, options, &context);
    same_graph(incremental, full); expect(incremental.stopped, "node budget did not cancel search");
   }
   limits.nodes = 0; std::atomic_bool stopped{true}; options.nnue_incremental = true;
   const auto fallback = search(board, limits, &stopped, {}, options, &context);
   options.nnue_incremental = false;
   same_graph(fallback, search(board, limits, &stopped, {}, options, &context));
   expect(fallback.depth == 0 && fallback.stopped, "pre-cancel fallback");
   stopped.store(false); options.nnue_incremental = true;
   const auto interrupted = search(board, limits, &stopped,
     [&](const auto&) { stopped.store(true); }, options, &context);
   stopped.store(false); options.nnue_incremental = false;
   same_graph(interrupted, search(board, limits, &stopped,
     [&](const auto&) { stopped.store(true); }, options, &context));
   expect(interrupted.depth == 1 && interrupted.stopped, "callback cancellation");
   limits.depth = 2; options.nnue_incremental = true;
   const auto resumed = search(board, limits, nullptr, {}, options, &context);
   options.nnue_incremental = false;
   same_graph(resumed, search(board, limits, nullptr, {}, options));
   expect(board == saved, "cancellation or reused context changed input history");
  }
 });
 test("search selection, fallback, terminal and context reuse", [] {
  Board board("7k/8/8/8/8/8/P7/6K1 w - - 0 1"); SearchLimits limits; limits.depth = 2;
  SearchOptions options; options.nnue = true; SearchContext context;
  const auto result = search(board, limits, nullptr, {}, options, &context);
  expect(result.best_move.has_value() && result.depth == 2, "neural search");
  options.positional = false;
  auto override_result = search(board, limits, nullptr, {}, options, &context);
  expect(result.score == override_result.score && result.pv == override_result.pv, "precedence");
  std::atomic_bool stop{true};
  expect(search(board, limits, &stop, {}, options).score == evaluate_neural(board), "fallback");
  options.nnue = false; options.positional = true;
  auto reused = search(board, limits, nullptr, {}, options, &context); auto fresh = search(board, limits);
  expect(reused.score == fresh.score && reused.pv == fresh.pv, "cache isolation");
  options.nnue = true;
  expect(search(Board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1"), limits, nullptr, {}, options).score == -mate_score, "mate");
 });
 test("integer NNUE shares LMR reductions, full-depth retries and cancellation exactly", [] {
  const Board board("4k3/8/2n2n2/3p4/3P4/2N2N2/8/4K3 w - - 0 1");
  const auto saved = board;
  auto fixed = std::make_shared<NeuralModel>(builtin_neural_model());
  fixed->format_version = 2; fixed->input_scale = 32; fixed->output_scale *= 32;
  for (auto& row : fixed->input_weights) for (int& weight : row) weight *= 32;
  for (int& bias : fixed->hidden_bias) bias *= 32;
  for (bool fast : {false, true}) for (bool table : {false, true}) {
   SearchOptions options; options.nnue = true; options.nnue_model = fixed;
   options.fast_moves = fast; options.transposition = table; options.hash_mb = 1;
   options.quiescence = false; options.lmr = true;
   SearchLimits limits; limits.depth = 5; SearchContext context;
   std::vector<SearchResult> incremental_iterations, full_iterations;
   const auto incremental = search(board, limits, nullptr,
    [&](const auto& result) { incremental_iterations.push_back(result); }, options, &context);
   options.nnue_incremental = false;
   const auto full = search(board, limits, nullptr,
    [&](const auto& result) { full_iterations.push_back(result); }, options, &context);
   same_graph(incremental, full);
   expect(!incremental.stopped && incremental.depth == 5 && incremental.lmr_reductions > 0 &&
          incremental.lmr_researches > 0,
          "fixture did not exercise reduced and full-depth integer NNUE retries");
   expect(incremental_iterations.size() == full_iterations.size(), "LMR iteration count differs");
   for (std::size_t i = 0; i < incremental_iterations.size(); ++i)
    same_graph(incremental_iterations[i], full_iterations[i]);
   limits.nodes = incremental.nodes - 1;
   options.nnue_incremental = true;
   const auto interrupted = search(board, limits, nullptr, {}, options, &context);
   options.nnue_incremental = false;
   same_graph(interrupted, search(board, limits, nullptr, {}, options, &context));
   expect(interrupted.stopped && interrupted.lmr_reductions > 0, "LMR interruption was not exercised");
   limits.nodes = 0; options.nnue_incremental = true;
   same_graph(incremental, search(board, limits, nullptr, {}, options, &context));
   options.root_moves = std::vector<Move>{board.legal_moves().front()};
   const auto restricted = search(board, limits, nullptr, {}, options, &context);
   options.nnue_incremental = false;
   same_graph(restricted, search(board, limits, nullptr, {}, options, &context));
   expect(restricted.best_move == options.root_moves->front(), "NNUE ignored restricted root");
   expect(board == saved, "LMR or restricted-root search altered original board/history");
  }
 });
 test("classical, residual and integer model transitions isolate reusable memory", [] {
  std::stringstream text;
  text << "REGICIDE_NN_V2 768 32 16 600 100\n";
  for (std::size_t i = 0; i < NeuralEvaluator::parameter_count; ++i)
   text << (i + 1 == NeuralEvaluator::parameter_count ? 1 : 0) << ' ';
  const auto residual = NeuralEvaluator::load(text);
  auto integer_a = std::make_shared<NeuralModel>(builtin_neural_model());
  auto integer_b = std::make_shared<NeuralModel>(*integer_a);
  integer_b->output_weights[0] += 37;
  const Board board("4k3/8/2n2n2/3p4/3P4/2N2N2/8/4K3 w - - 0 1");
  const auto saved = board;
  SearchOptions classical; classical.hash_mb = 1; classical.quiescence = false;
  SearchOptions correction = classical; correction.neural = residual;
  // Merely retaining a loaded integer model must not activate it.
  correction.nnue_model = integer_a;
  SearchOptions material = correction; material.positional = false;
  SearchOptions integer = classical; integer.nnue = true; integer.nnue_model = integer_a;
  SearchOptions other_integer = integer; other_integer.nnue_model = integer_b;
  SearchOptions full_integer = integer; full_integer.nnue_incremental = false;
  SearchContext context; SearchLimits limits; limits.depth = 3;
  for (const auto& options : {classical, correction, material, integer, other_integer,
                              full_integer, correction, classical})
   same_graph(search(board, limits, nullptr, {}, options, &context),
              search(board, limits, nullptr, {}, options));
  SearchOptions bare_material = classical; bare_material.positional = false;
  same_graph(search(board, limits, nullptr, {}, material),
             search(board, limits, nullptr, {}, bare_material));
  std::atomic_bool stopped{true};
  expect(search(board, limits, &stopped, {}, correction).score == residual->evaluate(board),
         "residual fallback no longer uses the installed evaluator");
  expect(search(board, limits, &stopped, {}, material).score == evaluate_material(board),
         "positional=false no longer overrides installed residual evaluation");
  SearchOptions invalid = integer; invalid.neural = residual;
  bool rejected = false;
  try { (void)search(board, limits, nullptr, {}, invalid, &context); }
  catch (const std::invalid_argument&) { rejected = true; }
  expect(rejected, "simultaneous residual and NNUE evaluation was not rejected");
  invalid.positional = false; rejected = false;
  try { (void)search(Board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1"), limits, nullptr, {}, invalid); }
  catch (const std::invalid_argument&) { rejected = true; }
  expect(rejected, "ambiguous modes bypassed validation at a terminal root");
  same_graph(search(board, limits, nullptr, {}, classical, &context),
             search(board, limits, nullptr, {}, classical));
  expect(board == saved, "evaluator switching or rejection changed input board/history");
 });
 std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
 return failures ? 1 : 0;
}
