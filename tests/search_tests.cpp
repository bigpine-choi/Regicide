#include "regicide/search.hpp"

#include <algorithm>
#include <atomic>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>

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
    catch (const std::exception& e) {
        ++failures;
        std::cerr << "FAIL " << name << ": " << e.what() << '\n';
    }
}
template<class F> void rejects(F action, const std::string& message) {
    bool threw = false;
    try { action(); } catch (const std::invalid_argument&) { threw = true; }
    expect(threw, message);
}
bool legal(const Board& board, const std::optional<Move>& move) {
    if (!move) return false;
    const auto moves = board.legal_moves();
    return std::find(moves.begin(), moves.end(), *move) != moves.end();
}
void valid_pv(const Board& input, const SearchResult& result) {
    Board board = input;
    expect(!result.pv.empty(), "missing principal variation");
    expect(result.best_move == result.pv.front(), "PV starts with a different move");
    expect(result.pv.size() <= static_cast<std::size_t>(std::max(1, result.depth)), "PV exceeds depth");
    for (Move move : result.pv) {
        expect(board.outcome() == Outcome::ongoing, "PV continues past terminal state");
        expect(legal(board, move), "PV contains an illegal move");
        board.play(move);
    }
}
void cycle(Board& board) {
    for (const char* move : {"g1f3", "g8f6", "f3g1", "f6g8"}) board.play(move);
}

// Deliberately unpruned oracle on small positions, independent of search windows.
int minimax(Board& board, int depth, int ply = 0) {
    const auto outcome = board.outcome();
    if (outcome == Outcome::checkmate) return -mate_score + ply;
    if (outcome != Outcome::ongoing) return 0;
    if (!depth) return evaluate_material(board);
    int best = -32000;
    for (Move move : board.legal_moves()) {
        board.play(move);
        const int score = -minimax(board, depth - 1, ply + 1);
        board.undo();
        best = std::max(best, score);
    }
    return best;
}
} // namespace

int main() {
    test("centipawn material and side-to-move perspective", [] {
        expect(evaluate_material(Board{}) == 0, "initial material must be equal");
        expect(evaluate_material(Board("7k/8/8/8/8/8/PNBRQ3/K7 w - - 0 1")) == 2150,
               "piece values must be P100 N320 B330 R500 Q900 K0");
        expect(evaluate_material(Board("7k/8/8/8/8/8/PNBRQ3/K7 b - - 0 1")) == -2150,
               "black perspective must reverse score");
    });
    test("both colors select a free queen", [] {
        for (const char* fen : {"7k/8/8/8/8/8/q7/R5K1 w - - 0 1",
                                "r5k1/Q7/8/8/8/8/8/7K b - - 0 1"}) {
            Board board(fen);
            const auto saved = board;
            const auto result = search(board, {.depth = 1});
            expect(result.best_move == Move::from_uci(board.position().turn == Color::white ? "a1a2" : "a8a7"),
                   "failed to capture a free queen");
            expect(result.score == 500, "rook should remain after queen capture");
            expect(result.depth == 1 && !result.stopped, "depth one should finish");
            expect(board == saved, "search changed original board/history");
            valid_pv(board, result);
        }
    });
    test("depth two sees the recapture beyond a tempting material gain", [] {
        Board board("7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1");
        const auto shallow = search(board, {.depth = 1});
        const auto deeper = search(board, {.depth = 2});
        expect(shallow.best_move == Move::from_uci("d1d5"), "depth one should see the free-looking rook");
        expect(deeper.best_move != Move::from_uci("d1d5"), "depth two must avoid the pawn recapture");
        expect(deeper.score >= 300, "depth two should retain the queen advantage");
        valid_pv(board, deeper);
    });
    test("mate on the horizon beats capturing material for both colors", [] {
        for (const char* fen : {"7k/q4Q2/6K1/8/8/8/8/8 w - - 0 1",
                                "8/8/8/8/8/6k1/Q4q2/7K b - - 0 1"}) {
            Board board(fen);
            const auto result = search(board, {.depth = 1});
            expect(legal(board, result.best_move), "mate search returned no legal move");
            expect(result.score == mate_score - 1, "mate in one requires distance score 29999");
            board.play(*result.best_move);
            expect(board.outcome() == Outcome::checkmate, "selected move does not mate");
        }
    });
    test("terminal roots distinguish mate and automatic draws", [] {
        Board mate("7k/6Q1/6K1/8/8/8/8/8 b - - 150 1");
        const auto loss = search(mate, {.depth = 3});
        expect(!loss.best_move && loss.pv.empty(), "mated root must not return a move");
        expect(loss.score == -mate_score && !loss.stopped, "mate must take priority over 75 moves");
        for (const char* fen : {"7k/5Q2/6K1/8/8/8/8/8 b - - 0 1",
                                "7k/8/8/8/8/8/8/K7 w - - 0 1",
                                "7k/8/8/8/8/8/8/R5K1 w - - 150 1"}) {
            const auto draw = search(Board(fen), {.depth = 2});
            expect(!draw.best_move && draw.pv.empty(), "terminal draw must not return a move");
            expect(draw.score == 0 && !draw.stopped, "terminal draw score");
        }
        Board repeated;
        for (int count = 0; count < 4; ++count) cycle(repeated);
        const auto draw = search(repeated, {.depth = 2});
        expect(!draw.best_move && draw.score == 0, "fivefold is automatic");
    });
    test("draw claims are optional and preserve a legal root move", [] {
        for (int halfmove : {99, 100}) {
            Board losing("7k/8/8/8/8/8/q7/6K1 w - - " + std::to_string(halfmove) + " 1");
            const auto result = search(losing, {.depth = 2});
            expect(result.score == 0, "current or intended 50-move claim avoids a loss");
            expect(legal(losing, result.best_move), "claimable root still needs a UCI move");
            valid_pv(losing, result);
        }
        Board winning("7k/5Q2/6K1/8/8/8/8/8 w - - 100 1");
        const auto win = search(winning, {.depth = 1});
        expect(win.score == mate_score - 1 && legal(winning, win.best_move),
               "a claim must not force a winning position to draw");
        Board horizon("7k/8/q7/8/8/8/8/1R4K1 b - - 98 1");
        const auto anticipated = search(horizon, {.depth = 1});
        expect(anticipated.score == 0 && legal(horizon, anticipated.best_move),
               "a losing horizon node can claim before its own intended 100th halfmove");
        const auto exact = search(horizon, {.depth = 1, .nodes = anticipated.nodes});
        expect(exact.depth == 1 && !exact.stopped && exact.nodes == anticipated.nodes,
               "claim scanning must finish the last budgeted node without adding nodes");
        Board repeated;
        cycle(repeated); cycle(repeated);
        const auto result = search(repeated, {.depth = 2});
        expect(repeated.claims_now().threefold, "fixture needs a threefold claim");
        expect(result.score >= 0 && legal(repeated, result.best_move), "threefold must be optional");
    });
    test("promotion and en passant use material correctly", [] {
        Board promotion("7k/P7/6K1/8/8/8/8/8 w - - 0 1");
        const auto promoted = search(promotion, {.depth = 1});
        expect(promoted.best_move == Move::from_uci("a7a8q"), "should select queen promotion");
        expect(promoted.score == mate_score - 1, "promotion gives mate");
        Board en_passant("7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1");
        const auto captured = search(en_passant, {.depth = 1});
        expect(captured.best_move == Move::from_uci("e5d6"), "should take en passant pawn");
        expect(captured.score == 100, "en passant should remove the enemy pawn");
    });
    test("alpha-beta matches unpruned minimax", [] {
        for (const char* fen : {"7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1",
                                "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
                                "7k/5Q2/6K1/8/8/8/8/8 w - - 0 1"}) {
            Board board(fen);
            const int expected = minimax(board, 2);
            const auto result = search(board, {.depth = 2});
            expect(result.score == expected, "alpha-beta score differs from minimax");
            valid_pv(board, result);
        }
    });
    test("deterministic iterative deepening and complete node counts", [] {
        Board board;
        int callbacks = 0;
        std::uint64_t previous_nodes = 0;
        const auto first = search(board, {.depth = 3}, nullptr, [&](const SearchResult& iteration) {
            ++callbacks;
            expect(iteration.depth == callbacks, "callback should follow each completed depth");
            expect(iteration.nodes > previous_nodes && !iteration.stopped, "callback work should increase");
            previous_nodes = iteration.nodes;
            valid_pv(board, iteration);
        });
        const auto second = search(board, {.depth = 3});
        expect(callbacks == 3 && first.depth == 3, "all requested iterations should finish");
        expect(first.best_move == second.best_move && first.score == second.score && first.pv == second.pv,
               "identical searches must return identical results");
        expect(first.nodes == second.nodes, "unlimited node counts must be deterministic");
        const auto one = search(board, {.depth = 1});
        expect(one.nodes == 21, "depth one counts the root and 20 leaves");
    });
    test("node budget returns last complete iteration and restores history", [] {
        Board board;
        cycle(board);
        const auto saved = board;
        const auto baseline = search(board, {.depth = 1});
        const auto result = search(board, {.depth = 4, .nodes = baseline.nodes + 1});
        expect(result.stopped && result.depth == 1, "interrupted depth must not be published");
        expect(result.nodes == baseline.nodes + 1, "node count must respect exact budget");
        expect(result.best_move == baseline.best_move && result.score == baseline.score && result.pv == baseline.pv,
               "incomplete iteration replaced the last complete result");
        expect(board == saved, "interrupted search changed board/repetition history");
        const auto fallback = search(board, {.depth = 4, .nodes = 1});
        expect(fallback.depth == 0 && fallback.nodes == 1 && fallback.stopped, "one node requires fallback");
        expect(legal(board, fallback.best_move), "fallback must be legal");
        valid_pv(board, fallback);
    });
    test("preexisting and between-iteration stop flags", [] {
        Board board;
        const auto saved = board;
        std::atomic_bool stop{true};
        const auto fallback = search(board, {.depth = 4}, &stop);
        expect(fallback.stopped && fallback.depth == 0 && fallback.nodes == 0, "stop must be polled before node one");
        expect(legal(board, fallback.best_move), "stop still requires a legal fallback");
        stop.store(false);
        const auto result = search(board, {.depth = 4}, &stop, [&](const SearchResult&) { stop.store(true); });
        expect(result.stopped && result.depth == 1 && result.nodes == 21, "stop after callback should preserve depth one");
        expect(board == saved, "stop changed original board/history");
    });
    test("wall clock budget and invalid limits", [] {
        Board board;
        const auto limited = search(board, {.depth = 64, .movetime = std::chrono::milliseconds(1)});
        expect(limited.stopped && legal(board, limited.best_move), "time-limited search must stop with a move");
        expect(limited.elapsed < std::chrono::seconds(2), "time polling overshot by more than two seconds");
        rejects([&] { (void)search(board, {.depth = 0}); }, "depth zero must be rejected");
        rejects([&] { (void)search(board, {.depth = 65}); }, "depth above 64 must be rejected");
        rejects([&] { (void)search(board, {.movetime = std::chrono::milliseconds(-1)}); }, "negative time must be rejected");
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
