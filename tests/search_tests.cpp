#include "regicide/search.hpp"
#include "regicide/evaluation.hpp"

#include <algorithm>
#include <atomic>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace regicide;
namespace {
SearchResult legacy_search(const Board& board, const SearchLimits& limits,
                           const std::atomic_bool* stop = nullptr,
                           SearchCallback callback = {}) {
    return search(board, limits, stop, std::move(callback), {false, false, false, 16, false});
}

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
    const int extension = result.qnodes ? max_quiescence_depth : 0;
    expect(result.pv.size() <= static_cast<std::size_t>(std::max(1, result.depth) + extension),
           "PV exceeds search and quiescence depth bound");
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
int minimax(Board& board, int depth, int ply = 0, bool positional = false) {
    const auto outcome = board.outcome();
    if (outcome == Outcome::checkmate) return -mate_score + ply;
    if (outcome != Outcome::ongoing) return 0;
    if (!depth) return positional ? evaluate_position(board) : evaluate_material(board);
    int best = -32000;
    for (Move move : board.legal_moves()) {
        board.play(move);
        const int score = -minimax(board, depth - 1, ply + 1, positional);
        board.undo();
        best = std::max(best, score);
    }
    return best;
}

// Unpruned tactical oracle: no windows, ordering, budget, or production helpers.
// These short fixtures have neither optional claims nor a long checking cycle.
int quiet_minimax(Board& board, int depth, int ply = 0) {
    const auto outcome = board.outcome();
    if (outcome == Outcome::checkmate) return -mate_score + ply;
    if (outcome != Outcome::ongoing) return 0;
    expect(ply < 20, "quiescence oracle fixture is too deep");
    const bool checked = board.in_check();
    int best = depth || checked ? -32000 : evaluate_material(board);
    const auto position = board.position();
    for (Move move : board.legal_moves()) {
        const char piece = position.squares[move.from];
        const bool ep = (piece == 'P' || piece == 'p') && move.to == position.en_passant;
        if (!depth && !checked && position.squares[move.to] == '.' && !ep && !move.promotion) continue;
        board.play(move);
        const int score = -quiet_minimax(board, std::max(0, depth - 1), ply + 1);
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
            const auto result = legacy_search(board, {.depth = 1});
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
        const auto shallow = legacy_search(board, {.depth = 1});
        const auto deeper = legacy_search(board, {.depth = 2});
        expect(shallow.best_move == Move::from_uci("d1d5"), "depth one should see the free-looking rook");
        expect(deeper.best_move != Move::from_uci("d1d5"), "depth two must avoid the pawn recapture");
        expect(deeper.score >= 300, "depth two should retain the queen advantage");
        valid_pv(board, deeper);
    });
    test("mate on the horizon beats capturing material for both colors", [] {
        for (const char* fen : {"7k/q4Q2/6K1/8/8/8/8/8 w - - 0 1",
                                "8/8/8/8/8/6k1/Q4q2/7K b - - 0 1"}) {
            Board board(fen);
            const auto result = legacy_search(board, {.depth = 1});
            expect(legal(board, result.best_move), "mate search returned no legal move");
            expect(result.score == mate_score - 1, "mate in one requires distance score 29999");
            board.play(*result.best_move);
            expect(board.outcome() == Outcome::checkmate, "selected move does not mate");
        }
    });
    test("terminal roots distinguish mate and automatic draws", [] {
        Board mate("7k/6Q1/6K1/8/8/8/8/8 b - - 150 1");
        const auto loss = legacy_search(mate, {.depth = 3});
        expect(!loss.best_move && loss.pv.empty(), "mated root must not return a move");
        expect(loss.score == -mate_score && !loss.stopped, "mate must take priority over 75 moves");
        for (const char* fen : {"7k/5Q2/6K1/8/8/8/8/8 b - - 0 1",
                                "7k/8/8/8/8/8/8/K7 w - - 0 1",
                                "7k/8/8/8/8/8/8/R5K1 w - - 150 1"}) {
            const auto draw = legacy_search(Board(fen), {.depth = 2});
            expect(!draw.best_move && draw.pv.empty(), "terminal draw must not return a move");
            expect(draw.score == 0 && !draw.stopped, "terminal draw score");
        }
        Board repeated;
        for (int count = 0; count < 4; ++count) cycle(repeated);
        const auto draw = legacy_search(repeated, {.depth = 2});
        expect(!draw.best_move && draw.score == 0, "fivefold is automatic");
    });
    test("draw claims are optional and preserve a legal root move", [] {
        for (int halfmove : {99, 100}) {
            Board losing("7k/8/8/8/8/8/q7/6K1 w - - " + std::to_string(halfmove) + " 1");
            const auto result = legacy_search(losing, {.depth = 2});
            expect(result.score == 0, "current or intended 50-move claim avoids a loss");
            expect(legal(losing, result.best_move), "claimable root still needs a UCI move");
            valid_pv(losing, result);
        }
        Board winning("7k/5Q2/6K1/8/8/8/8/8 w - - 100 1");
        const auto win = legacy_search(winning, {.depth = 1});
        expect(win.score == mate_score - 1 && legal(winning, win.best_move),
               "a claim must not force a winning position to draw");
        Board horizon("7k/8/q7/8/8/8/8/1R4K1 b - - 98 1");
        const auto anticipated = legacy_search(horizon, {.depth = 1});
        expect(anticipated.score == 0 && legal(horizon, anticipated.best_move),
               "a losing horizon node can claim before its own intended 100th halfmove");
        const auto exact = legacy_search(horizon, {.depth = 1, .nodes = anticipated.nodes});
        expect(exact.depth == 1 && !exact.stopped && exact.nodes == anticipated.nodes,
               "claim scanning must finish the last budgeted node without adding nodes");
        Board repeated;
        cycle(repeated); cycle(repeated);
        const auto result = legacy_search(repeated, {.depth = 2});
        expect(repeated.claims_now().threefold, "fixture needs a threefold claim");
        expect(result.score >= 0 && legal(repeated, result.best_move), "threefold must be optional");
    });
    test("promotion and en passant use material correctly", [] {
        Board promotion("7k/P7/6K1/8/8/8/8/8 w - - 0 1");
        const auto promoted = legacy_search(promotion, {.depth = 1});
        expect(promoted.best_move == Move::from_uci("a7a8q"), "should select queen promotion");
        expect(promoted.score == mate_score - 1, "promotion gives mate");
        Board en_passant("7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1");
        const auto captured = legacy_search(en_passant, {.depth = 1});
        expect(captured.best_move == Move::from_uci("e5d6"), "should take en passant pawn");
        expect(captured.score == 100, "en passant should remove the enemy pawn");
    });
    test("alpha-beta matches unpruned minimax", [] {
        for (const char* fen : {"7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1",
                                "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
                                "7k/5Q2/6K1/8/8/8/8/8 w - - 0 1"}) {
            Board board(fen);
            const int expected = minimax(board, 2);
            const auto result = legacy_search(board, {.depth = 2});
            expect(result.score == expected, "alpha-beta score differs from minimax");
            valid_pv(board, result);
        }
    });
    test("deterministic iterative deepening and complete node counts", [] {
        Board board;
        int callbacks = 0;
        std::uint64_t previous_nodes = 0;
        const auto first = legacy_search(board, {.depth = 3}, nullptr, [&](const SearchResult& iteration) {
            ++callbacks;
            expect(iteration.depth == callbacks, "callback should follow each completed depth");
            expect(iteration.nodes > previous_nodes && !iteration.stopped, "callback work should increase");
            previous_nodes = iteration.nodes;
            valid_pv(board, iteration);
        });
        const auto second = legacy_search(board, {.depth = 3});
        expect(callbacks == 3 && first.depth == 3, "all requested iterations should finish");
        expect(first.best_move == second.best_move && first.score == second.score && first.pv == second.pv,
               "identical searches must return identical results");
        expect(first.nodes == second.nodes, "unlimited node counts must be deterministic");
        const auto one = legacy_search(board, {.depth = 1});
        expect(one.nodes == 21, "depth one counts the root and 20 leaves");
    });
    test("node budget returns last complete iteration and restores history", [] {
        Board board;
        cycle(board);
        const auto saved = board;
        const auto baseline = legacy_search(board, {.depth = 1});
        const auto result = legacy_search(board, {.depth = 4, .nodes = baseline.nodes + 1});
        expect(result.stopped && result.depth == 1, "interrupted depth must not be published");
        expect(result.nodes == baseline.nodes + 1, "node count must respect exact budget");
        expect(result.best_move == baseline.best_move && result.score == baseline.score && result.pv == baseline.pv,
               "incomplete iteration replaced the last complete result");
        expect(board == saved, "interrupted search changed board/repetition history");
        const auto fallback = legacy_search(board, {.depth = 4, .nodes = 1});
        expect(fallback.depth == 0 && fallback.nodes == 1 && fallback.stopped, "one node requires fallback");
        expect(legal(board, fallback.best_move), "fallback must be legal");
        valid_pv(board, fallback);
    });
    test("preexisting and between-iteration stop flags", [] {
        Board board;
        const auto saved = board;
        std::atomic_bool stop{true};
        const auto fallback = legacy_search(board, {.depth = 4}, &stop);
        expect(fallback.stopped && fallback.depth == 0 && fallback.nodes == 0, "stop must be polled before node one");
        expect(legal(board, fallback.best_move), "stop still requires a legal fallback");
        stop.store(false);
        const auto result = legacy_search(board, {.depth = 4}, &stop, [&](const SearchResult&) { stop.store(true); });
        expect(result.stopped && result.depth == 1 && result.nodes == 21, "stop after callback should preserve depth one");
        expect(board == saved, "stop changed original board/history");
    });
    test("wall clock budget and invalid limits", [] {
        Board board;
        const auto limited = legacy_search(board, {.depth = 64, .movetime = std::chrono::milliseconds(1)});
        expect(limited.stopped && legal(board, limited.best_move), "time-limited search must stop with a move");
        expect(limited.elapsed < std::chrono::seconds(2), "time polling overshot by more than two seconds");
        rejects([&] { (void)legacy_search(board, {.depth = 0}); }, "depth zero must be rejected");
        rejects([&] { (void)legacy_search(board, {.depth = 65}); }, "depth above 64 must be rejected");
        rejects([&] { (void)legacy_search(board, {.movetime = std::chrono::milliseconds(-1)}); }, "negative time must be rejected");
    });
    test("quiescence avoids a poisoned rook at depth one", [] {
        Board board("7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1");
        const auto saved = board;
        const auto old = legacy_search(board, {.depth = 1});
        const auto quiet = search(board, {.depth = 1}, nullptr, {}, {false, true});
        expect(old.best_move == Move::from_uci("d1d5"), "baseline must fall for the poisoned rook");
        expect(quiet.best_move != old.best_move && quiet.score == 300,
               "quiescence must see the pawn recapture and retain the queen");
        expect(quiet.qnodes > 0 && quiet.nodes > quiet.qnodes && quiet.seldepth >= 2,
               "quiescence work must be included in total work");
        expect(board == saved, "quiescence altered the input board/history");
        valid_pv(board, quiet);
    });
    test("checked horizon searches quiet evasions without stand pat", [] {
        Board board("7k/P7/8/8/8/8/8/6K1 w - - 0 1");
        const auto result = search(board, {.depth = 1}, nullptr, {}, {false, true});
        expect(result.best_move == Move::from_uci("a7a8q") && result.score == 900,
               "queen promotion should preserve its material value");
        expect(result.pv.size() > 1 && result.seldepth > result.depth,
               "promotion check requires an evasion beyond the nominal depth");
        board.play(result.pv.front());
        expect(board.in_check(), "fixture must check at the quiescence frontier");
        expect(board.position().squares[result.pv[1].to] == '.', "evasion must be a quiet king move");
        board.undo();
        valid_pv(board, result);
    });
    test("quiescence recognizes en passant at the frontier", [] {
        Board board("r6k/3p4/8/4P3/8/8/8/6K1 b - - 0 1");
        const auto old = legacy_search(board, {.depth = 1});
        const auto result = search(board, {.depth = 1}, nullptr, {}, {false, true});
        expect(old.best_move == Move::from_uci("d7d5"), "baseline fixture must offer en passant");
        expect(result.best_move != Move::from_uci("d7d5") && result.score == 500,
               "frontier en passant should prevent sacrificing the pawn");
        valid_pv(board, result);
    });
    test("extended alpha-beta matches unpruned tactical minimax", [] {
        for (const char* fen : {"7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1",
                                "r6k/3p4/8/4P3/8/8/8/6K1 b - - 0 1",
                                "4r2k/8/8/8/8/8/4p3/R5K1 w - - 0 1",
                                "7k/P7/8/8/8/8/8/6K1 w - - 0 1"}) {
            Board board(fen);
            const auto saved = board;
            const int expected = quiet_minimax(board, 1);
            const auto result = search(board, {.depth = 1}, nullptr, {}, {false, true});
            expect(result.score == expected, "quiescence differs from unpruned tactical oracle");
            expect(board == saved, "oracle or production quiescence changed board/history");
            valid_pv(board, result);
        }
    });
    test("positional alpha-beta matches unpruned minimax without quiescence", [] {
        for (const char* fen : {"7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1",
                                "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1"}) {
            Board board(fen);
            const int expected = minimax(board, 2, 0, true);
            const auto result = search(board, {.depth = 2}, nullptr, {}, {true, false});
            expect(result.score == expected, "positional alpha-beta differs from minimax");
            expect(result.qnodes == 0, "disabled quiescence must do no quiescence work");
            valid_pv(board, result);
        }
    });
    test("quiet horizon is stable and counted only once", [] {
        Board board;
        const auto result = search(board, {.depth = 1}, nullptr, {}, {false, true});
        const auto old = legacy_search(board, {.depth = 1});
        expect(result.score == old.score && result.best_move == old.best_move && result.pv == old.pv,
               "a quiet initial horizon should match static material search");
        expect(result.nodes == 21 && result.qnodes == 20 && result.seldepth == 1,
               "root and quiet leaves must be counted exactly once");
        const auto default_result = search(board, {.depth = 2});
        const auto repeated = search(board, {.depth = 2});
        expect(default_result.best_move == repeated.best_move && default_result.pv == repeated.pv &&
                   default_result.score == repeated.score && default_result.nodes == repeated.nodes &&
                   default_result.qnodes == repeated.qnodes,
               "new default search must remain deterministic");
        valid_pv(board, default_result);
    });
    test("quiescence terminal and optional claim handling", [] {
        Board mate("7k/q4Q2/6K1/8/8/8/8/8 w - - 149 1");
        const auto win = search(mate, {.depth = 1}, nullptr, {}, {false, true});
        expect(win.score == mate_score - 1, "horizon mate must outrank the 75-move draw");
        mate.play(*win.best_move);
        expect(mate.outcome() == Outcome::checkmate, "horizon mate score must correspond to mate");
        Board horizon("7k/8/q7/8/8/8/8/1R4K1 b - - 98 1");
        const auto claim = search(horizon, {.depth = 1}, nullptr, {}, {false, true});
        expect(claim.score == 0 && legal(horizon, claim.best_move),
               "quiet intended 50-move claim must remain available at a q frontier");
        Board repeated;
        cycle(repeated); cycle(repeated);
        const auto draw = search(repeated, {.depth = 2});
        expect(draw.score >= 0 && legal(repeated, draw.best_move), "threefold claim remains optional");
        Board stalemate("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1");
        const auto stale = search(stalemate, {.depth = 1});
        expect(stale.score == 0 && !stale.best_move, "stalemate must not be a static material win");
    });
    test("quiescence shares exact node budgets and restores interrupted work", [] {
        Board board("7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1");
        const auto saved = board;
        const auto complete = search(board, {.depth = 1}, nullptr, {}, {false, true});
        const auto exact = search(board, {.depth = 1, .nodes = complete.nodes}, nullptr, {}, {false, true});
        expect(!exact.stopped && exact.depth == 1 && exact.nodes == complete.nodes &&
                   exact.qnodes == complete.qnodes,
               "exact quiescence budget must finish without double counting");
        const auto interrupted = search(board, {.depth = 3, .nodes = complete.nodes + 2},
                                        nullptr, {}, {false, true});
        expect(interrupted.stopped && interrupted.depth == 1 && interrupted.nodes == complete.nodes + 2,
               "unfinished quiescence must obey the shared total node ceiling");
        expect(interrupted.best_move == complete.best_move && interrupted.score == complete.score &&
                   interrupted.pv == complete.pv,
               "unfinished q iteration must preserve the last complete result");
        expect(interrupted.qnodes <= interrupted.nodes && board == saved,
               "q cancellation must restore repetition history");
        const auto early = search(board, {.depth = 2, .nodes = 3}, nullptr, {}, {false, true});
        expect(early.depth == 0 && early.nodes == 3 && early.qnodes == 2 && early.stopped,
               "interrupting inside the first q extension requires a legal fallback");
        expect(legal(board, early.best_move) && board == saved, "early q interruption changed board");
        valid_pv(board, early);
    });
    test("transposition scores agree with uncached fixed-depth search", [] {
        for (const char* fen : {"7k/6pp/8/8/8/8/PP6/K7 w - - 0 1",
                                "7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1",
                                "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
                                "7k/5Q2/6K1/8/8/8/8/8 w - - 0 1"}) {
            for (bool quiet : {false, true}) {
                Board board(fen);
                const auto saved = board;
                const auto uncached = search(board, {.depth = 3}, nullptr, {}, {true, quiet, false, 1});
                const auto cached = search(board, {.depth = 3}, nullptr, {}, {true, quiet, true, 1});
                expect(!cached.stopped && cached.depth == 3 && cached.score == uncached.score,
                       "table changed fixed-depth result");
                expect(!uncached.tt_probes && !uncached.tt_hits && !uncached.tt_cutoffs && !uncached.tt_stores,
                       "disabled table must do no cache work");
                expect(cached.tt_probes > 0 && cached.tt_stores > 0 &&
                           cached.tt_cutoffs <= cached.tt_hits && cached.tt_hits <= cached.tt_probes,
                       "table counters are inconsistent");
                expect(board == saved, "cached search changed board or repetition context");
                valid_pv(board, cached);
            }
        }
    });
    test("transpositions reuse completed work and never publish a partial root", [] {
        Board board("7k/6pp/8/8/8/8/PP6/K7 w - - 0 1");
        const auto saved = board;
        const auto uncached = search(board, {.depth = 5}, nullptr, {}, {true, false, false, 1});
        const auto cached = search(board, {.depth = 5}, nullptr, {}, {true, false, true, 1});
        expect(cached.score == uncached.score && cached.depth == 5 && !cached.stopped,
               "reusing a transposition changed the completed score");
        expect(cached.tt_hits > 0 && cached.tt_cutoffs > 0, "fixture must exercise actual score cutoffs");
        expect(cached.nodes < uncached.nodes, "fixture must reduce visited nodes");
        valid_pv(board, cached);
        const auto quiet_plain = search(board, {.depth = 5}, nullptr, {}, {true, true, false, 1});
        const auto quiet_cached = search(board, {.depth = 5}, nullptr, {}, {true, true, true, 1});
        expect(quiet_cached.score == quiet_plain.score && quiet_cached.depth == 5 &&
                   quiet_cached.tt_cutoffs > 0 && quiet_cached.qnodes > 0,
               "normal-node cache reuse must preserve a quiescence frontier");
        valid_pv(board, quiet_cached);
        const auto interrupted = search(board, {.depth = 5, .nodes = 1}, nullptr, {}, {true, true, true, 1});
        expect(interrupted.stopped && interrupted.depth == 0 && interrupted.tt_stores == 0,
               "unfinished first root must never be stored or published");
        expect(legal(board, interrupted.best_move) && board == saved, "interruption changed root or fallback");
        const auto repeated = search(board, {.depth = 5}, nullptr, {}, {true, false, true, 1});
        expect(repeated.score == cached.score && repeated.pv == cached.pv && repeated.nodes == cached.nodes &&
                   repeated.tt_hits == cached.tt_hits && repeated.tt_stores == cached.tt_stores,
               "table must be search-local and deterministic across calls");
    });
    test("cached search keeps optional history-dependent claims", [] {
        Board repeated;
        cycle(repeated); cycle(repeated);
        Board other_history;
        for (const char* move : {"b1c3", "b8c6", "c3b1", "c6b8"}) other_history.play(move);
        for (Board board : {repeated, other_history,
                            Board("7k/8/8/8/8/8/q7/6K1 w - - 99 1"),
                            Board("7k/5Q2/6K1/8/8/8/8/8 w - - 149 1")}) {
            const auto saved = board;
            const auto plain = search(board, {.depth = 2}, nullptr, {}, {true, true, false, 1});
            const auto cached = search(board, {.depth = 2}, nullptr, {}, {true, true, true, 1});
            expect(cached.score == plain.score && cached.depth == 2,
                   "table changed mate precedence or an optional draw claim");
            expect(board == saved, "history-dependent cached search changed input");
            valid_pv(board, cached);
        }
        rejects([&] { (void)search(repeated, {.depth = 1}, nullptr, {}, {true, true, true, 0}); },
                "zero hash size must be rejected");
        rejects([&] { (void)search(repeated, {.depth = 1}, nullptr, {}, {true, true, true, 1025}); },
                "unbounded hash size must be rejected");
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
