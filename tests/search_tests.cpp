#include "regicide/search.hpp"
#include "regicide/evaluation.hpp"
#include "regicide/see.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace regicide;
namespace {
// Every pre-LMR regression retains the full-depth search it was written for.
// LMR is selective, so its dedicated tests below verify the new contract
// without weakening exhaustive-oracle or exact search-graph assertions.
SearchResult reference_search(const Board& board, const SearchLimits& limits,
                              const std::atomic_bool* stop = nullptr,
                              SearchCallback callback = {}, SearchOptions options = {},
                              SearchContext* context = nullptr) {
    options.lmr = false;
    return search(board, limits, stop, std::move(callback), options, context);
}

// Keep every pre-PVS/SEE regression on its original full-window search graph.
// Dedicated tests below exercise both modes without relaxing old invariants.
SearchResult alpha_beta_search(const Board& board, const SearchLimits& limits,
                               const std::atomic_bool* stop = nullptr,
                               SearchCallback callback = {}, SearchOptions options = {},
                               SearchContext* context = nullptr) {
    options.pvs = false;
    options.see_ordering = false;
    return reference_search(board, limits, stop, std::move(callback), options, context);
}
SearchResult legacy_search(const Board& board, const SearchLimits& limits,
                           const std::atomic_bool* stop = nullptr,
                           SearchCallback callback = {}) {
    return alpha_beta_search(board, limits, stop, std::move(callback), {false, false, false, 16, false, false, false});
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
void same_graph(const SearchResult& left, const SearchResult& right) {
    expect(left.best_move == right.best_move && left.score == right.score &&
               left.depth == right.depth && left.pv == right.pv && left.stopped == right.stopped,
           "reused memory changed the selected move, score, PV or completed depth");
    expect(left.nodes == right.nodes && left.qnodes == right.qnodes && left.seldepth == right.seldepth &&
               left.tt_probes == right.tt_probes && left.tt_hits == right.tt_hits &&
               left.tt_cutoffs == right.tt_cutoffs && left.tt_stores == right.tt_stores &&
               left.pvs_probes == right.pvs_probes && left.pvs_researches == right.pvs_researches &&
               left.see_calls == right.see_calls && left.lmr_reductions == right.lmr_reductions &&
               left.lmr_researches == right.lmr_researches,
           "reused memory changed the search graph or transposition work");
}
void no_memory_work(const SearchResult& result) {
    expect(!result.tt_allocations && !result.tt_reuses && !result.tt_clears && !result.tt_bytes,
           "disabled or terminal search must do no transposition memory work");
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

struct CheckOracleCoverage {
    int quiet_checks = 0;
    int zero_budget_evasions = 0;
    int zero_budget_counterchecks = 0;
    int captures_with_budget = 0;
    int promotions_with_budget = 0;
};

// Independent bounded-check oracle: enumerate every legal move and inspect the
// resulting check state with play(), without the generated-check helper,
// pruning, ordering, cache or production qsearch code.
int check_minimax(Board& board, int depth, int checks_left, CheckOracleCoverage& coverage,
                  int ply = 0) {
    const auto outcome = board.outcome();
    if (outcome == Outcome::checkmate) return -mate_score + ply;
    if (outcome != Outcome::ongoing) return 0;
    expect(ply < 20, "bounded-check oracle fixture is too deep");
    const bool checked = board.in_check();
    int best = depth || checked ? -32000 : evaluate_material(board);
    const auto position = board.position();
    for (Move move : board.legal_moves()) {
        const char piece = position.squares[move.from];
        const bool capture = position.squares[move.to] != '.' ||
            ((piece == 'P' || piece == 'p') && move.to == position.en_passant);
        const bool tactical = capture || move.promotion;
        board.play(move);
        const bool gives_check = board.in_check();
        if (!depth && !checked && !tactical && !(checks_left && gives_check)) {
            board.undo();
            continue;
        }
        int child_budget = checks_left;
        if (!depth) {
            if (checked && checks_left == 0) {
                ++coverage.zero_budget_evasions;
                if (!tactical && gives_check) ++coverage.zero_budget_counterchecks;
            }
            if (!tactical && gives_check) {
                ++coverage.quiet_checks;
                child_budget = std::max(0, checks_left - 1);
            }
            if (capture && checks_left) ++coverage.captures_with_budget;
            if (move.promotion && checks_left) ++coverage.promotions_with_budget;
        }
        const int score = -check_minimax(board, std::max(0, depth - 1), child_budget, coverage, ply + 1);
        board.undo();
        best = std::max(best, score);
    }
    return best;
}

SearchOptions pvs_options(bool enabled, bool table, bool positional = false,
                          bool quiet = false, bool checks = false) {
    return {positional, quiet, table, 1, true, true, checks, enabled, false};
}

SearchOptions see_options(bool enabled, bool pvs, bool table,
                          bool quiet = false, bool checks = false) {
    auto options = pvs_options(pvs, table, false, quiet, checks);
    options.see_ordering = enabled;
    return options;
}

struct OptimalMoves {
    int score = -32000;
    std::vector<Move> moves;
};

// Enumerate every root alternative so tied choices remain valid, while a
// unique optimum must actually be selected. No alpha-beta bounds are used.
OptimalMoves optimal_moves(Board& board, int depth, bool positional) {
    OptimalMoves result;
    for (Move move : board.legal_moves()) {
        board.play(move);
        const int score = -minimax(board, depth - 1, 1, positional);
        board.undo();
        if (score > result.score) { result.score = score; result.moves.clear(); }
        if (score == result.score) result.moves.push_back(move);
    }
    return result;
}

bool has_claim(DrawClaims claims) { return claims.threefold || claims.fifty_moves; }

// This oracle asks the public rules API about each possible intended claim;
// it does not duplicate the searcher's history shortcuts or score windows.
int claim_minimax(Board& board, int depth, bool positional, int ply = 0) {
    const auto outcome = board.outcome();
    if (outcome == Outcome::checkmate) return -mate_score + ply;
    if (outcome != Outcome::ongoing) return 0;
    const bool current = has_claim(board.claims_now());
    const auto moves = board.legal_moves();
    if (!depth) {
        const int score = positional ? evaluate_position(board) : evaluate_material(board);
        if (score >= 0) return score;
        if (current) return 0;
        for (Move move : moves) if (has_claim(board.claims_after(move))) return 0;
        return score;
    }
    int best = current ? 0 : -32000;
    for (Move move : moves) {
        const bool intended = has_claim(board.claims_after(move));
        board.play(move);
        int score = -claim_minimax(board, depth - 1, positional, ply + 1);
        board.undo();
        if (intended) score = std::max(0, score);
        best = std::max(best, score);
    }
    return best;
}

void same_completed(const SearchResult& interrupted, const SearchResult& complete) {
    expect(interrupted.depth == complete.depth && interrupted.best_move == complete.best_move &&
               interrupted.score == complete.score && interrupted.pv == complete.pv,
           "unfinished PVS pass replaced the last complete iteration");
}

// A separate exhaustive oracle for the configurable position evaluator. At
// quiet frontiers it keeps every legal capture/promotion and every check
// evasion, but has no alpha-beta windows, ordering, TT, budget, or SEE helper.
int shelter_minimax(Board& board, int depth, bool shield, bool quiet, int ply = 0,
                    bool structure = false) {
    const auto outcome = board.outcome();
    if (outcome == Outcome::checkmate) return -mate_score + ply;
    if (outcome != Outcome::ongoing) return 0;
    expect(ply < 20, "shelter oracle fixture must have a finite short tactical frontier");
    if (!depth && !quiet) return evaluate_position(board, shield, structure);
    const bool frontier = depth == 0;
    const bool checked = board.in_check();
    int best = frontier && !checked ? evaluate_position(board, shield, structure) : -32000;
    const auto position = board.position();
    for (Move move : board.legal_moves()) {
        const char piece = position.squares[move.from];
        const bool capture = position.squares[move.to] != '.' ||
            ((piece == 'P' || piece == 'p') && move.to == position.en_passant);
        if (frontier && !checked && !capture && !move.promotion) continue;
        board.play(move);
        const int score = -shelter_minimax(board, std::max(0, depth - 1), shield, quiet, ply + 1, structure);
        board.undo();
        best = std::max(best, score);
    }
    return best;
}

OptimalMoves shelter_optimal_moves(Board& board, int depth, bool shield, bool quiet,
                                   bool structure = false) {
    OptimalMoves result;
    for (Move move : board.legal_moves()) {
        board.play(move);
        const int score = -shelter_minimax(board, depth - 1, shield, quiet, 1, structure);
        board.undo();
        if (score > result.score) { result.score = score; result.moves.clear(); }
        if (score == result.score) result.moves.push_back(move);
    }
    return result;
}

SearchOptions shelter_options(bool shield, bool quiet, bool table, bool structure = false) {
    SearchOptions options;
    options.positional = true;
    options.quiescence = quiet;
    options.quiescence_checks = false;
    options.transposition = table;
    options.hash_mb = 1;
    options.king_pawn_shield = shield;
    options.pawn_structure = structure;
    return options;
}

SearchOptions lmr_options(bool enabled, bool table = true) {
    SearchOptions options;
    options.hash_mb = 1;
    options.quiescence = false;
    options.quiescence_checks = false;
    options.transposition = table;
    options.lmr = enabled;
    return options;
}

constexpr const char* lmr_quiet_fixture =
    "4k3/8/2n2n2/3p4/3P4/2N2N2/8/4K3 w - - 0 1";

void lmr_counts(const SearchResult& result) {
    expect(result.lmr_researches <= result.lmr_reductions,
           "one reduced scout cannot require more than one normal-depth verification");
    expect(result.pvs_probes >= result.lmr_reductions + result.lmr_researches,
           "LMR reduced and verification scouts must be included in actual PVS work");
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
        const auto quiet = alpha_beta_search(board, {.depth = 1}, nullptr, {}, {false, true});
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
        const auto result = alpha_beta_search(board, {.depth = 1}, nullptr, {}, {false, true});
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
        const auto result = alpha_beta_search(board, {.depth = 1}, nullptr, {}, {false, true});
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
            const auto result = alpha_beta_search(board, {.depth = 1}, nullptr, {}, {false, true, true, 16, true, false, false});
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
            const auto result = alpha_beta_search(board, {.depth = 2}, nullptr, {}, {true, false});
            expect(result.score == expected, "positional alpha-beta differs from minimax");
            expect(result.qnodes == 0, "disabled quiescence must do no quiescence work");
            valid_pv(board, result);
        }
    });
    test("quiet horizon is stable and counted only once", [] {
        Board board;
        const auto result = alpha_beta_search(board, {.depth = 1}, nullptr, {}, {false, true});
        const auto old = legacy_search(board, {.depth = 1});
        expect(result.score == old.score && result.best_move == old.best_move && result.pv == old.pv,
               "a quiet initial horizon should match static material search");
        expect(result.nodes == 21 && result.qnodes == 20 && result.seldepth == 1,
               "root and quiet leaves must be counted exactly once");
        const auto default_result = alpha_beta_search(board, {.depth = 2});
        const auto repeated = alpha_beta_search(board, {.depth = 2});
        expect(default_result.best_move == repeated.best_move && default_result.pv == repeated.pv &&
                   default_result.score == repeated.score && default_result.nodes == repeated.nodes &&
                   default_result.qnodes == repeated.qnodes,
               "new default search must remain deterministic");
        valid_pv(board, default_result);
    });
    test("quiescence terminal and optional claim handling", [] {
        Board mate("7k/q4Q2/6K1/8/8/8/8/8 w - - 149 1");
        const auto win = alpha_beta_search(mate, {.depth = 1}, nullptr, {}, {false, true});
        expect(win.score == mate_score - 1, "horizon mate must outrank the 75-move draw");
        mate.play(*win.best_move);
        expect(mate.outcome() == Outcome::checkmate, "horizon mate score must correspond to mate");
        Board horizon("7k/8/q7/8/8/8/8/1R4K1 b - - 98 1");
        const auto claim = alpha_beta_search(horizon, {.depth = 1}, nullptr, {}, {false, true});
        expect(claim.score == 0 && legal(horizon, claim.best_move),
               "quiet intended 50-move claim must remain available at a q frontier");
        Board repeated;
        cycle(repeated); cycle(repeated);
        const auto draw = alpha_beta_search(repeated, {.depth = 2});
        expect(draw.score >= 0 && legal(repeated, draw.best_move), "threefold claim remains optional");
        Board stalemate("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1");
        const auto stale = alpha_beta_search(stalemate, {.depth = 1});
        expect(stale.score == 0 && !stale.best_move, "stalemate must not be a static material win");
    });
    test("quiescence shares exact node budgets and restores interrupted work", [] {
        Board board("7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1");
        const auto saved = board;
        const auto complete = alpha_beta_search(board, {.depth = 1}, nullptr, {}, {false, true});
        const auto exact = alpha_beta_search(board, {.depth = 1, .nodes = complete.nodes}, nullptr, {}, {false, true});
        expect(!exact.stopped && exact.depth == 1 && exact.nodes == complete.nodes &&
                   exact.qnodes == complete.qnodes,
               "exact quiescence budget must finish without double counting");
        const auto interrupted = alpha_beta_search(board, {.depth = 3, .nodes = complete.nodes + 2},
                                        nullptr, {}, {false, true});
        expect(interrupted.stopped && interrupted.depth == 1 && interrupted.nodes == complete.nodes + 2,
               "unfinished quiescence must obey the shared total node ceiling");
        expect(interrupted.best_move == complete.best_move && interrupted.score == complete.score &&
                   interrupted.pv == complete.pv,
               "unfinished q iteration must preserve the last complete result");
        expect(interrupted.qnodes <= interrupted.nodes && board == saved,
               "q cancellation must restore repetition history");
        const auto early = alpha_beta_search(board, {.depth = 2, .nodes = 3}, nullptr, {}, {false, true});
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
                const auto uncached = alpha_beta_search(board, {.depth = 3}, nullptr, {}, {true, quiet, false, 1});
                const auto cached = alpha_beta_search(board, {.depth = 3}, nullptr, {}, {true, quiet, true, 1});
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
        const auto uncached = alpha_beta_search(board, {.depth = 5}, nullptr, {}, {true, false, false, 1});
        const auto cached = alpha_beta_search(board, {.depth = 5}, nullptr, {}, {true, false, true, 1});
        expect(cached.score == uncached.score && cached.depth == 5 && !cached.stopped,
               "reusing a transposition changed the completed score");
        expect(cached.tt_hits > 0 && cached.tt_cutoffs > 0, "fixture must exercise actual score cutoffs");
        expect(cached.nodes < uncached.nodes, "fixture must reduce visited nodes");
        valid_pv(board, cached);
        const auto quiet_plain = alpha_beta_search(board, {.depth = 5}, nullptr, {}, {true, true, false, 1});
        const auto quiet_cached = alpha_beta_search(board, {.depth = 5}, nullptr, {}, {true, true, true, 1});
        expect(quiet_cached.score == quiet_plain.score && quiet_cached.depth == 5 &&
                   quiet_cached.tt_cutoffs > 0 && quiet_cached.qnodes > 0,
               "normal-node cache reuse must preserve a quiescence frontier");
        valid_pv(board, quiet_cached);
        const auto interrupted = alpha_beta_search(board, {.depth = 5, .nodes = 1}, nullptr, {}, {true, true, true, 1});
        expect(interrupted.stopped && interrupted.depth == 0 && interrupted.tt_stores == 0,
               "unfinished first root must never be stored or published");
        expect(legal(board, interrupted.best_move) && board == saved, "interruption changed root or fallback");
        const auto repeated = alpha_beta_search(board, {.depth = 5}, nullptr, {}, {true, false, true, 1});
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
            const auto plain = alpha_beta_search(board, {.depth = 2}, nullptr, {}, {true, true, false, 1});
            const auto cached = alpha_beta_search(board, {.depth = 2}, nullptr, {}, {true, true, true, 1});
            expect(cached.score == plain.score && cached.depth == 2,
                   "table changed mate precedence or an optional draw claim");
            expect(board == saved, "history-dependent cached search changed input");
            valid_pv(board, cached);
        }
        rejects([&] { (void)alpha_beta_search(repeated, {.depth = 1}, nullptr, {}, {true, true, true, 0}); },
                "zero hash size must be rejected");
        rejects([&] { (void)alpha_beta_search(repeated, {.depth = 1}, nullptr, {}, {true, true, true, 1025}); },
                "unbounded hash size must be rejected");
    });
    test("caller-owned memory reuse preserves every completed iteration graph", [] {
        Board board("7k/6pp/8/8/8/8/PP6/K7 w - - 0 1");
        const SearchOptions options{true, true, true, 1};
        SearchContext context;
        context.clear(); // A new context and Clear Hash must not preallocate.
        std::vector<SearchResult> first_iterations;
        const auto first = alpha_beta_search(board, {.depth = 4}, nullptr,
                                  [&](const SearchResult& result) { first_iterations.push_back(result); },
                                  options, &context);
        expect(first.tt_allocations == 1 && first.tt_reuses == 0 && first.tt_clears == 0 &&
                   first.tt_bytes > 0 && first.tt_bytes <= 1024 * 1024,
               "first active search must account for one bounded allocation");
        std::vector<SearchResult> reused_iterations;
        const auto reused = alpha_beta_search(board, {.depth = 4}, nullptr,
                                   [&](const SearchResult& result) { reused_iterations.push_back(result); },
                                   options, &context);
        expect(reused.tt_allocations == 0 && reused.tt_reuses == 1 && reused.tt_clears == 0 &&
                   reused.tt_bytes == first.tt_bytes, "second search must reuse the same bounded storage");
        same_graph(first, reused);
        expect(first_iterations.size() == 4 && reused_iterations.size() == first_iterations.size(),
               "all iterative results must remain available");
        for (std::size_t i = 0; i < first_iterations.size(); ++i) {
            same_graph(first_iterations[i], reused_iterations[i]);
            expect(first_iterations[i].tt_allocations == 1 && reused_iterations[i].tt_reuses == 1,
                   "iteration callbacks must carry this call's memory work");
        }
        const auto fresh = alpha_beta_search(board, {.depth = 4}, nullptr, {}, options);
        same_graph(reused, fresh);
        expect(fresh.tt_allocations == 1 && !fresh.tt_reuses,
               "null-context callers must retain one-shot allocation behavior");
        context.clear();
        const auto after_clear = alpha_beta_search(board, {.depth = 4}, nullptr, {}, options, &context);
        same_graph(fresh, after_clear);
        expect(!after_clear.tt_allocations && after_clear.tt_reuses == 1 && !after_clear.tt_clears,
               "explicit clear must retain allocation and start another fresh graph");
        valid_pv(board, after_clear);
    });
    test("reused memory isolates options FENs history clocks and hash resize", [] {
        SearchContext context;
        Board knight_g, knight_b;
        cycle(knight_g);
        for (const char* move : {"b1c3", "b8c6", "c3b1", "c6b8"}) knight_b.play(move);
        Board repeated = knight_g;
        cycle(repeated);
        std::vector<Board> positions{
            knight_g, knight_b, Board(knight_g.fen()), repeated,
            Board("7k/8/8/8/8/8/q7/6K1 w - - 98 1"),
            Board("7k/8/8/8/8/8/q7/6K1 w - - 99 1"),
            Board("7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1"),
            Board("7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1")};
        const std::vector<SearchOptions> options{
            {true, true, true, 1, true}, {false, false, true, 1, false},
            {true, false, true, 1, true}, {false, true, true, 1, true},
            {true, true, false, 1, true}, {true, true, true, 2, true},
            {true, true, true, 1, true}};
        int retained_mb = 0;
        for (const auto& position : positions) {
            const auto saved = position;
            for (const auto& option : options) {
                const auto fresh = alpha_beta_search(position, {.depth = 2}, nullptr, {}, option);
                const auto reused = alpha_beta_search(position, {.depth = 2}, nullptr, {}, option, &context);
                same_graph(fresh, reused);
                if (option.transposition) {
                    const bool resize = retained_mb != option.hash_mb;
                    expect(reused.tt_allocations == (resize ? 1U : 0U) &&
                               reused.tt_reuses == (resize ? 0U : 1U) && reused.tt_clears == 0,
                           "only actual hash-size changes may allocate storage");
                    expect(reused.tt_bytes == fresh.tt_bytes &&
                               reused.tt_bytes <= static_cast<std::uint64_t>(option.hash_mb) * 1024 * 1024,
                           "retained storage must obey the current Hash budget after a shrink");
                    retained_mb = option.hash_mb;
                } else {
                    no_memory_work(reused);
                }
                valid_pv(position, reused);
                expect(position == saved, "shared context changed input state/history");
            }
        }
    });
    test("disabled terminal and interrupted calls preserve reusable storage safely", [] {
        SearchContext context;
        Board board("7k/6pp/8/8/8/8/PP6/K7 w - - 0 1");
        const SearchOptions options{true, true, true, 1};
        const auto expected = alpha_beta_search(board, {.depth = 3}, nullptr, {}, options, &context);
        const auto saved = board;
        const auto disabled = alpha_beta_search(board, {.depth = 1}, nullptr, {}, {true, true, false, 2}, &context);
        no_memory_work(disabled);
        for (const char* fen : {"7k/6Q1/6K1/8/8/8/8/8 b - - 0 1",
                                "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1",
                                "7k/8/8/8/8/8/8/K7 w - - 0 1"}) {
            const auto terminal = alpha_beta_search(Board(fen), {.depth = 3}, nullptr, {}, {true, true, true, 2}, &context);
            no_memory_work(terminal);
            expect(!terminal.best_move && terminal.pv.empty(), "terminal root must remain terminal");
        }
        const auto interrupted = alpha_beta_search(board, {.depth = 5, .nodes = expected.nodes + 2},
                                        nullptr, {}, options, &context);
        expect(interrupted.stopped && interrupted.tt_reuses == 1 && interrupted.tt_allocations == 0 &&
                   interrupted.tt_bytes == expected.tt_bytes, "interruption must not discard retained storage");
        valid_pv(board, interrupted);
        std::atomic_bool stop{true};
        const auto stopped = alpha_beta_search(board, {.depth = 5}, &stop, {}, options, &context);
        expect(stopped.stopped && stopped.nodes == 0 && stopped.tt_reuses == 1,
               "preexisting stop must still produce a legal fallback with bounded reuse");
        valid_pv(board, stopped);
        const auto restarted = alpha_beta_search(board, {.depth = 3}, nullptr, {}, options, &context);
        same_graph(expected, restarted);
        expect(restarted.tt_reuses == 1 && !restarted.tt_allocations &&
                   restarted.tt_bytes == expected.tt_bytes && board == saved,
               "disabled/terminal/interrupted calls must preserve allocation without leaking scores");
        SearchContext independent;
        const auto other_worker = alpha_beta_search(board, {.depth = 3}, nullptr, {}, options, &independent);
        same_graph(restarted, other_worker);
        expect(other_worker.tt_allocations == 1 && !other_worker.tt_reuses,
               "independent worker contexts must own independent allocations");
    });
    test("quiet move ordering changes work without changing exact fixed-depth scores", [] {
        for (const char* fen : {"7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1",
                                "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1"}) {
            Board board(fen);
            const auto saved = board;
            const int expected = minimax(board, 2, 0, true);
            const auto old = alpha_beta_search(board, {.depth = 2}, nullptr, {}, {true, false, false, 1, true, false, false});
            const auto ordered = alpha_beta_search(board, {.depth = 2}, nullptr, {}, {true, false, false, 1, true, true, false});
            expect(old.score == expected && ordered.score == expected,
                   "killer/history ordering must preserve unpruned minimax value");
            expect(board == saved, "quiet ordering changed board or history");
            valid_pv(board, ordered);
        }
        Board initial;
        const auto old = alpha_beta_search(initial, {.depth = 4}, nullptr, {}, {true, false, false, 1, true, false, false});
        const auto ordered = alpha_beta_search(initial, {.depth = 4}, nullptr, {}, {true, false, false, 1, true, true, false});
        expect(old.score == ordered.score && old.depth == 4 && ordered.depth == 4,
               "ordering must not change completed initial-position minimax value");
        expect(ordered.nodes < old.nodes, "initial fixture must exercise useful quiet cutoffs");
        valid_pv(initial, ordered);
        SearchContext context;
        const auto fresh = alpha_beta_search(initial, {.depth = 3}, nullptr, {}, {true, false, true, 1, true, true, false}, &context);
        (void)alpha_beta_search(initial, {.depth = 4, .nodes = 50}, nullptr, {},
                     {true, false, true, 1, true, false, false}, &context);
        const auto repeated = alpha_beta_search(initial, {.depth = 3}, nullptr, {}, {true, false, true, 1, true, true, false}, &context);
        same_graph(fresh, repeated);
    });
    test("one quiet-check qsearch matches an unpruned independent oracle", [] {
        CheckOracleCoverage total;
        for (const char* fen : {"7k/8/2b5/8/8/5q2/r4P1P/R5K1 w - - 0 1",
                                "1k6/8/7b/8/8/8/2R5/K7 w - - 0 1",
                                "r6k/3p4/8/4P3/8/8/8/6K1 b - - 0 1",
                                "6k1/8/8/8/8/8/p7/6KR w - - 0 1"}) {
            Board board(fen);
            const auto saved = board;
            CheckOracleCoverage coverage;
            const int expected = check_minimax(board, 1, 1, coverage);
            const auto cached = alpha_beta_search(board, {.depth = 1}, nullptr, {}, {false, true, true, 1, true, true, true});
            const auto plain = alpha_beta_search(board, {.depth = 1}, nullptr, {}, {false, true, false, 1, true, false, true});
            expect(cached.score == expected && plain.score == expected && !cached.stopped && !plain.stopped,
                   "bounded quiet checks must preserve the independent tactical value");
            expect(board == saved, "quiet check generation or recursion changed original state");
            valid_pv(board, cached);
            total.quiet_checks += coverage.quiet_checks;
            total.zero_budget_evasions += coverage.zero_budget_evasions;
            total.zero_budget_counterchecks += coverage.zero_budget_counterchecks;
            total.captures_with_budget += coverage.captures_with_budget;
            total.promotions_with_budget += coverage.promotions_with_budget;
        }
        expect(total.quiet_checks > 0 && total.zero_budget_evasions > 0 &&
                   total.zero_budget_counterchecks > 0,
               "fixtures must cover quiet checks and all evasions including counterchecks at zero budget");
        expect(total.captures_with_budget > 0 && total.promotions_with_budget > 0,
               "fixtures must cover captures and promotions preserving the quiet-check budget");
    });
    test("quiet checks recognize mate beyond the capture-only frontier", [] {
        Board board("7k/8/2b5/8/8/5q2/r4P1P/R5K1 w - - 0 1");
        const auto capture_only = alpha_beta_search(board, {.depth = 1}, nullptr, {}, {false, true, true, 1, true, true, false});
        const auto checking = alpha_beta_search(board, {.depth = 1}, nullptr, {}, {false, true, true, 1, true, true, true});
        expect(capture_only.score > -mate_score + max_search_ply,
               "capture-only fixture must miss the quiet mating move");
        expect(checking.score == -mate_score + 2 && checking.pv.size() == 2,
               "one quiet checking move must reveal mate on the second ply");
        Board line = board;
        line.play(checking.pv.front());
        expect(line.position().squares[checking.pv[1].to] == '.', "mating move must be a non-capture");
        line.play(checking.pv[1]);
        expect(line.outcome() == Outcome::checkmate, "quiet-check mate must be verified on the board");
        valid_pv(board, checking);
    });
    test("Caro-Kann loss regression sees the checking knight refutation", [] {
        Board board("1k2r2r/p3b1p1/5p2/3qnN2/Pp4P1/8/1PQB1P1P/2R3KR w - - 0 30");
        const auto saved = board;
        const auto old = alpha_beta_search(board, {.depth = 2}, nullptr, {}, {true, true, true, 1, true, true, false});
        const auto checking = alpha_beta_search(board, {.depth = 2}, nullptr, {}, {true, true, true, 1, true, true, true});
        expect(old.best_move == Move::from_uci("f5e7"), "recorded loss fixture must still tempt Nxe7");
        expect(checking.best_move != old.best_move && checking.depth == 2 && !checking.stopped,
               "quiet checking continuation must change the tempting capture choice");
        expect(board == saved, "tactical extension changed the regression position");
        valid_pv(board, checking);
        const auto no_cache = alpha_beta_search(board, {.depth = 2}, nullptr, {}, {true, true, false, 1, true, true, true});
        expect(no_cache.score == checking.score, "normal-node TT must preserve the new qsearch value");
        SearchContext context;
        (void)alpha_beta_search(board, {.depth = 2}, nullptr, {}, {true, true, true, 1, true, true, false}, &context);
        const auto reused = alpha_beta_search(board, {.depth = 2}, nullptr, {}, {true, true, true, 1, true, true, true}, &context);
        same_graph(checking, reused);
        const auto interrupted = alpha_beta_search(board, {.depth = 3, .nodes = 100}, nullptr, {},
                                        {true, true, true, 1, true, true, true}, &context);
        expect(interrupted.stopped && interrupted.nodes == 100 && legal(board, interrupted.best_move) && board == saved,
               "quiet-check extension must share exact cancellation budget and restore history");
        valid_pv(board, interrupted);
    });
    test("PVS and alpha-beta select oracle-optimal moves with and without TT", [] {
        int unique = 0, tied = 0;
        std::uint64_t probes = 0, researches = 0;
        for (const char* fen : {"7k/8/8/8/8/8/q7/R5K1 w - - 0 1",
                                "r5k1/Q7/8/8/8/8/8/7K b - - 0 1",
                                "7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1",
                                "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
                                "7k/6pp/8/8/8/8/PP6/K7 w - - 0 1",
                                "7k/q4Q2/6K1/8/8/8/8/8 w - - 0 1",
                                "8/8/8/8/8/6k1/Q4q2/7K b - - 0 1"}) {
            for (bool positional : {false, true}) {
                Board board(fen);
                const auto saved = board;
                const auto expected = optimal_moves(board, 2, positional);
                expect(!expected.moves.empty(), "oracle requires a nonterminal fixture");
                expected.moves.size() == 1 ? ++unique : ++tied;
                for (bool table : {false, true}) for (bool enabled : {false, true}) {
                    const auto result = reference_search(board, {.depth = 2}, nullptr, {},
                                               pvs_options(enabled, table, positional));
                    expect(result.depth == 2 && !result.stopped && result.score == expected.score,
                           "PVS/TT changed the independent fixed-depth minimax score");
                    expect(result.best_move && std::find(expected.moves.begin(), expected.moves.end(),
                                                         *result.best_move) != expected.moves.end(),
                           "selected move is outside the oracle's optimal set");
                    expect(board == saved, "PVS or oracle changed complete input board/history");
                    expect(result.pvs_researches <= result.pvs_probes,
                           "every PVS re-search must follow a scout");
                    if (!enabled) expect(!result.pvs_probes && !result.pvs_researches,
                                         "disabled PVS must do no scout work");
                    probes += result.pvs_probes;
                    researches += result.pvs_researches;
                    valid_pv(board, result);
                }
            }
        }
        expect(unique > 0 && tied > 0, "oracle fixtures must exercise unique and tied optima");
        expect(probes > 0 && researches > 0, "fixtures must actually enter both PVS passes");
    });
    test("PVS preserves independently enumerated quiescence and quiet-check values", [] {
        for (const char* fen : {"7k/8/2b5/8/8/5q2/r4P1P/R5K1 w - - 0 1",
                                "1k6/8/7b/8/8/8/2R5/K7 w - - 0 1",
                                "r6k/3p4/8/4P3/8/8/8/6K1 b - - 0 1",
                                "6k1/8/8/8/8/8/p7/6KR w - - 0 1"}) {
            Board board(fen);
            const auto saved = board;
            CheckOracleCoverage coverage;
            const int expected = check_minimax(board, 1, 1, coverage);
            for (bool table : {false, true}) for (bool enabled : {false, true}) {
                const auto result = reference_search(board, {.depth = 1}, nullptr, {},
                                           pvs_options(enabled, table, false, true, true));
                expect(!result.stopped && result.depth == 1 && result.score == expected,
                       "PVS changed the independent bounded-check tactical value");
                expect(result.qnodes > 0 && result.qnodes <= result.nodes,
                       "scout/re-search quiescence must share the total node counter");
                expect(board == saved, "PVS tactical search changed state/history");
                valid_pv(board, result);
            }
        }
    });
    test("PVS scouts ties without re-search and re-searches a genuine improvement", [] {
        Board board;
        const auto saved = board;
        const auto tied = reference_search(board, {.depth = 3}, nullptr, {}, pvs_options(true, false));
        expect(tied.score == 0 && tied.pvs_probes > 0 && tied.pvs_researches == 0,
               "equal material leaves should exercise scouts without full-window re-search");
        const auto developed = reference_search(board, {.depth = 1}, nullptr, {}, pvs_options(true, false, true));
        const auto expected = optimal_moves(board, 1, true);
        expect(developed.score == expected.score && developed.pvs_researches > 0,
               "later superior knight development must trigger a full-window re-search");
        expect(board == saved, "PVS pass coverage changed the input");
        valid_pv(board, tied);
        valid_pv(board, developed);
    });
    test("PVS handles current intended and historical optional draw claims", [] {
        Board current("7k/8/8/8/8/q7/8/6K1 w - - 0 1");
        for (int i = 0; i < 2; ++i)
            for (const char* move : {"g1h1", "h8g8", "h1g1", "g8h8"}) current.play(move);
        expect(current.claims_now().threefold, "fixture must offer a current repetition claim");
        Board intended("7k/8/8/8/8/q7/8/6K1 b - - 0 1");
        for (const char* move : {"h8g8", "g1h1", "g8h8", "h1g1", "h8g8", "g1h1", "g8h8"})
            intended.play(move);
        expect(!intended.claims_now().threefold && intended.claims_after(Move::from_uci("h1g1")).threefold,
               "fixture must offer an intended-only repetition claim");
        for (Board board : {current, intended,
                            Board("7k/8/8/8/8/8/q7/6K1 w - - 99 1"),
                            Board("7k/8/8/8/8/8/q7/6K1 w - - 100 1"),
                            Board("7k/8/q7/8/8/8/8/1R4K1 b - - 98 1"),
                            Board("7k/5Q2/6K1/8/8/8/8/8 w - - 149 1")}) {
            const auto saved = board;
            const int expected = claim_minimax(board, 2, false);
            for (bool table : {false, true}) for (bool enabled : {false, true}) {
                const auto result = reference_search(board, {.depth = 2}, nullptr, {}, pvs_options(enabled, table));
                expect(!result.stopped && result.depth == 2 && result.score == expected,
                       "PVS lost an optional claim or mate precedence");
                expect(board == saved, "draw-claim scout/re-search changed repetition history");
                valid_pv(board, result);
            }
        }
    });
    test("an intended draw floor can require a PVS full-window re-search", [] {
        Board board("7k/8/3q4/3p4/8/8/8/3R2K1 w - - 99 1");
        const auto saved = board;
        const int expected = claim_minimax(board, 1, false);
        expect(expected == 0 && !has_claim(board.claims_after(Move::from_uci("d1d5"))),
               "capture must reset the clock while later quiet moves can claim a draw");
        for (bool table : {false, true}) {
            auto options = pvs_options(true, table);
            options.quiet_ordering = false;
            const auto result = reference_search(board, {.depth = 1}, nullptr, {}, options);
            expect(result.score == expected && result.best_move != Move::from_uci("d1d5"),
                   "intended claim must improve on the first negative capture score");
            expect(result.pvs_researches > 0,
                   "claim-adjusted scout improvement must be re-searched before publishing its PV");
            expect(board == saved, "intended-claim re-search changed the input");
            valid_pv(board, result);
        }
    });
    test("PVS interruption at scout and re-search entry retains the completed iteration", [] {
        Board board;
        cycle(board);
        const auto saved = board;
        for (bool table : {false, true}) {
            const auto options = pvs_options(true, table, true);
            const auto one = reference_search(board, {.depth = 1}, nullptr, {}, options);
            const auto two = reference_search(board, {.depth = 2}, nullptr, {}, options);
            expect(two.pvs_probes > one.pvs_probes && two.pvs_researches > one.pvs_researches,
                   "second iteration must exercise new scouts and re-searches");
            for (bool research : {false, true}) {
                const auto count = [research](const SearchResult& result) {
                    return research ? result.pvs_researches : result.pvs_probes;
                };
                // Node budgets make the prefix deterministic. Find the exact
                // first entry to a new pass after depth one has completed.
                std::uint64_t low = one.nodes + 1, high = two.nodes - 1;
                while (low < high) {
                    const auto middle = low + (high - low) / 2;
                    const auto prefix = reference_search(board, {.depth = 2, .nodes = middle}, nullptr, {}, options);
                    if (count(prefix) > count(one)) high = middle;
                    else low = middle + 1;
                }
                std::vector<SearchResult> published;
                const auto interrupted = reference_search(board, {.depth = 2, .nodes = low}, nullptr,
                    [&](const SearchResult& result) { published.push_back(result); }, options);
                expect(interrupted.stopped && interrupted.nodes == low && count(interrupted) > count(one),
                       "node cancellation must actually enter the selected PVS pass");
                expect(published.size() == 1, "unfinished PVS iteration reached the callback");
                same_completed(interrupted, one);
                same_completed(published.front(), one);
                expect(board == saved, "cancelled PVS pass changed complete board/history");
                valid_pv(board, interrupted);
            }
            const auto exact = reference_search(board, {.depth = 2, .nodes = two.nodes}, nullptr, {}, options);
            same_graph(exact, two);
            expect(!exact.stopped, "an exact complete PVS node budget must finish");
            std::atomic_bool stop{true};
            const auto fallback = reference_search(board, {.depth = 3}, &stop, {}, options);
            expect(fallback.stopped && fallback.depth == 0 && !fallback.nodes &&
                       !fallback.pvs_probes && !fallback.pvs_researches,
                   "preexisting stop must prevent entering either PVS pass");
            valid_pv(board, fallback);
        }
    });
    test("reused PVS memory isolates modes histories options and interruption", [] {
        SearchContext context;
        Board repeated;
        cycle(repeated); cycle(repeated);
        const std::vector<Board> positions{repeated, Board(repeated.fen()),
            Board("7k/8/8/8/8/8/q7/6K1 w - - 99 1"),
            Board("7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1")};
        for (const auto& board : positions) {
            const auto saved = board;
            for (bool enabled : {true, false, true}) for (bool quiet : {false, true}) {
                const auto options = pvs_options(enabled, true, true, quiet, quiet);
                const auto fresh = reference_search(board, {.depth = 2}, nullptr, {}, options);
                const auto reused = reference_search(board, {.depth = 2}, nullptr, {}, options, &context);
                same_graph(fresh, reused);
                (void)reference_search(board, {.depth = 3, .nodes = 7}, nullptr, {},
                             pvs_options(!enabled, true, false, !quiet), &context);
                const auto restarted = reference_search(board, {.depth = 2}, nullptr, {}, options, &context);
                same_graph(fresh, restarted);
                expect(restarted.tt_reuses == 1 && !restarted.tt_allocations && board == saved,
                       "PVS mode or incomplete prior search leaked through retained storage");
                valid_pv(board, restarted);
            }
        }
    });
    test("SEE ordering preserves independently enumerated optimal moves in every PVS/TT mode", [] {
        std::uint64_t calls = 0;
        for (const char* fen : {"7k/8/8/8/8/8/q7/R5K1 w - - 0 1",
                                "r5k1/Q7/8/8/8/8/8/7K b - - 0 1",
                                "7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1",
                                "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
                                "1rr5/P6k/8/8/8/8/8/7K w - - 0 1"}) {
            Board board(fen);
            const auto saved = board;
            const auto expected = optimal_moves(board, 2, false);
            for (bool enabled : {false, true}) for (bool pvs : {false, true})
                for (bool table : {false, true}) {
                    const auto result = reference_search(board, {.depth = 2}, nullptr, {},
                                               see_options(enabled, pvs, table));
                    expect(result.depth == 2 && !result.stopped && result.score == expected.score,
                           "SEE sorting changed the full minimax value");
                    expect(result.best_move && std::find(expected.moves.begin(), expected.moves.end(),
                                                         *result.best_move) != expected.moves.end(),
                           "SEE sorting selected a non-optimal legal root move");
                    expect(board == saved, "SEE ordering changed complete board/history");
                    if (!enabled) expect(result.see_calls == 0, "disabled SEE performed exchange work");
                    calls += result.see_calls;
                    valid_pv(board, result);
                }
        }
        expect(calls > 0, "SEE mode never evaluated a tactical candidate");
    });
    test("a sole legal capture needs no exchange sort and still preserves its result", [] {
        Board board("8/8/8/8/8/8/r1k5/K7 w - - 0 1");
        const auto saved = board;
        const auto moves = board.legal_moves();
        expect(moves.size() == 1 && moves.front() == Move::from_uci("a1a2"),
               "fixture must have exactly one legal capture evasion");
        for (bool pvs : {false, true}) for (bool table : {false, true}) {
            const auto result = reference_search(board, {.depth = 2}, nullptr, {}, see_options(true, pvs, table));
            const auto old_order = reference_search(board, {.depth = 2}, nullptr, {}, see_options(false, pvs, table));
            expect(result.depth == 2 && !result.stopped && result.score == 0 &&
                       result.best_move == moves.front() && !result.see_calls,
                   "forced capture should be searched without SEE work");
            same_graph(result, old_order);
            expect(board == saved, "forced capture ordering changed board or history");
            valid_pv(board, result);
        }
    });
    test("favorable captures precede losing captures without discarding either", [] {
        // a4 is outside the defending rook's fifth rank, so Rxa4 is truly free.
        Board board("7k/8/4p3/3r4/p7/8/8/R2Q2K1 w - - 0 1");
        const auto saved = board;
        expect(static_exchange_evaluation(board, Move::from_uci("a1a4")) == 100 &&
                   static_exchange_evaluation(board, Move::from_uci("d1d5")) == -400,
               "fixture must offer a free pawn and a poisoned rook");
        auto old_options = see_options(false, true, false);
        old_options.quiet_ordering = false;
        auto new_options = old_options;
        new_options.see_ordering = true;
        const auto old = reference_search(board, {.depth = 1}, nullptr, {}, old_options);
        const auto ordered = reference_search(board, {.depth = 1}, nullptr, {}, new_options);
        // At a deliberately unextended material horizon the poisoned rook is
        // worth more. It must still be searched after the favorable pawn and
        // improve the first candidate, triggering a full-window PVS re-search.
        expect(old.best_move == Move::from_uci("d1d5") && ordered.best_move == old.best_move &&
                   old.score == ordered.score && old.pvs_researches == 0 && ordered.pvs_researches > 0,
               "favorable capture did not lead, or a losing exchange was pruned");
        expect(ordered.see_calls > 0 && old.see_calls == 0 && board == saved,
               "SEE cost or input-preservation contract failed");
        valid_pv(board, ordered);
    });
    test("negative SEE queen sacrifice remains available and produces the oracle mate", [] {
        Board board("5rnk/6pp/7N/3Q4/8/8/8/K7 w - - 0 1");
        const auto saved = board;
        const auto sacrifice = Move::from_uci("d5g8");
        expect(static_exchange_evaluation(board, sacrifice) < 0,
               "smothered-mate fixture must sacrifice material in its local exchange");
        const auto expected = optimal_moves(board, 3, false);
        expect(expected.score == mate_score - 3 &&
                   std::find(expected.moves.begin(), expected.moves.end(), sacrifice) != expected.moves.end(),
               "unpruned oracle must see the sacrifice's forced mate");
        for (bool pvs : {false, true}) for (bool table : {false, true}) {
            const auto result = reference_search(board, {.depth = 3}, nullptr, {}, see_options(true, pvs, table));
            expect(result.score == expected.score && result.best_move &&
                       std::find(expected.moves.begin(), expected.moves.end(), *result.best_move) != expected.moves.end(),
                   "negative SEE suppressed the mating sacrifice in normal search");
            // The capture-only/quiet-check frontier also must retain the
            // negative capture; it is an ordering hint, never a qsearch filter.
            const auto tactical = reference_search(board, {.depth = 1}, nullptr, {},
                                         see_options(true, pvs, table, true, true));
            expect(tactical.score == mate_score - 3 && tactical.best_move == sacrifice,
                   "negative SEE suppressed the mating sacrifice at the tactical frontier");
            expect(board == saved, "sacrifice search changed board/history");
            valid_pv(board, result);
            valid_pv(board, tactical);
        }
    });
    test("SEE preserves the independent bounded-check quiescence oracle", [] {
        for (const char* fen : {"7k/8/2b5/8/8/5q2/r4P1P/R5K1 w - - 0 1",
                                "1k6/8/7b/8/8/8/2R5/K7 w - - 0 1",
                                "r6k/3p4/8/4P3/8/8/8/6K1 b - - 0 1",
                                "6k1/8/8/8/8/8/p7/6KR w - - 0 1"}) {
            Board board(fen);
            const auto saved = board;
            CheckOracleCoverage coverage;
            const int expected = check_minimax(board, 1, 1, coverage);
            for (bool pvs : {false, true}) for (bool table : {false, true}) {
                const auto result = reference_search(board, {.depth = 1}, nullptr, {},
                                           see_options(true, pvs, table, true, true));
                expect(!result.stopped && result.depth == 1 && result.score == expected,
                       "SEE ordering changed independent tactical/check-evasion value");
                expect(board == saved, "SEE tactical sort changed input state/history");
                valid_pv(board, result);
            }
        }
    });
    test("SEE mode changes and cancellation cannot leak through retained memory", [] {
        Board repeated;
        cycle(repeated);
        SearchContext context;
        for (const Board& board : {repeated,
                                  Board("3r3k/8/8/3pP3/8/8/8/3R2K1 w - d6 0 1")}) {
            const auto saved = board;
            for (bool enabled : {true, false, true}) for (bool pvs : {false, true}) {
                auto options = see_options(enabled, pvs, true, true, true);
                const auto fresh = reference_search(board, {.depth = 2}, nullptr, {}, options);
                const auto reused = reference_search(board, {.depth = 2}, nullptr, {}, options, &context);
                same_graph(fresh, reused);
                options.see_ordering = !enabled;
                const auto cancelled = reference_search(board, {.depth = 4, .nodes = 20}, nullptr, {}, options, &context);
                expect(cancelled.stopped && cancelled.nodes == 20 && legal(board, cancelled.best_move),
                       "SEE ordering bypassed the shared cancellation budget");
                options.see_ordering = enabled;
                const auto restarted = reference_search(board, {.depth = 2}, nullptr, {}, options, &context);
                same_graph(fresh, restarted);
                expect(restarted.tt_reuses == 1 && !restarted.tt_allocations && board == saved,
                       "SEE change or incomplete search leaked through retained memory");
                std::atomic_bool stop{true};
                const auto stopped = reference_search(board, {.depth = 3}, &stop, {}, options, &context);
                expect(stopped.stopped && !stopped.nodes && !stopped.see_calls,
                       "preexisting stop should avoid SEE work in fallback ordering");
                valid_pv(board, reused);
                valid_pv(board, cancelled);
                valid_pv(board, stopped);
            }
        }
    });
    test("shelter ON and OFF search match exhaustive normal and tactical oracles", [] {
        bool changed_value = false;
        std::uint64_t tactical_nodes = 0;
        for (const char* fen : {"3q3k/6pp/8/8/8/8/P5PP/4R1K1 w - - 0 1",
                                "7k/8/4p3/3r4/8/8/5PPP/3Q2K1 w - - 0 1",
                                "3q2k1/5ppp/8/8/3R4/4P3/8/7K b - - 0 1"}) {
            Board board(fen);
            const auto saved = board;
            for (bool quiet : {false, true}) {
                const int depth = quiet ? 1 : 2;
                const auto previous = shelter_optimal_moves(board, depth, false, quiet);
                const auto current = shelter_optimal_moves(board, depth, true, quiet);
                changed_value |= previous.score != current.score;
                for (bool shield : {false, true}) for (bool table : {false, true}) {
                    const auto& expected = shield ? current : previous;
                    const auto result = reference_search(board, {.depth = depth}, nullptr, {},
                                               shelter_options(shield, quiet, table));
                    expect(!result.stopped && result.depth == depth && result.score == expected.score,
                           "shelter flag was not applied consistently to normal or tactical leaves");
                    expect(result.best_move && std::find(expected.moves.begin(), expected.moves.end(),
                                                         *result.best_move) != expected.moves.end(),
                           "shelter search selected a move outside the exhaustive optimal set");
                    expect(quiet ? result.qnodes > 0 : result.qnodes == 0,
                           "shelter fixture did not exercise its requested frontier");
                    tactical_nodes += result.qnodes;
                    expect(board == saved, "shelter search or oracle changed board/history");
                    valid_pv(board, result);
                }
            }
        }
        expect(changed_value && tactical_nodes > 0,
               "new search fixtures must observe a real shelter-score difference and tactical work");
    });
    test("cancelled fallback uses the selected shelter evaluator before any completed iteration", [] {
        Board board("3q3k/6pp/8/8/8/8/P5PP/4R1K1 w - - 0 1");
        const auto saved = board;
        expect(evaluate_position(board, true, false) != evaluate_position(board, false, false),
               "fallback fixture must distinguish the two position evaluators");
        for (bool shield : {false, true}) for (bool quiet : {false, true}) {
            const auto options = shelter_options(shield, quiet, true);
            std::atomic_bool stop{true};
            const auto stopped = reference_search(board, {.depth = 3}, &stop, {}, options);
            expect(stopped.stopped && stopped.depth == 0 && !stopped.nodes &&
                       stopped.score == evaluate_position(board, shield, false),
                   "pre-stopped fallback lost the shelter mode or searched a node");
            const auto limited = reference_search(board, {.depth = 3, .nodes = 1}, nullptr, {}, options);
            expect(limited.stopped && limited.depth == 0 && limited.nodes == 1 &&
                       limited.score == evaluate_position(board, shield, false),
                   "node-limited fallback used a different static evaluator");
            expect(board == saved, "shelter fallback changed board or history");
            valid_pv(board, stopped); valid_pv(board, limited);
        }
    });
    test("reused memory isolates shelter mode switches and material-only evaluation ignores them", [] {
        Board repeated;
        cycle(repeated); cycle(repeated);
        SearchContext context;
        for (const Board& board : {Board("3q3k/6pp/8/8/8/8/P5PP/4R1K1 w - - 0 1"), repeated}) {
            const auto saved = board;
            for (bool shield : {true, false, true}) for (bool quiet : {false, true}) {
                auto options = shelter_options(shield, quiet, true);
                const auto fresh = reference_search(board, {.depth = 2}, nullptr, {}, options);
                const auto reused = reference_search(board, {.depth = 2}, nullptr, {}, options, &context);
                same_graph(fresh, reused);
                options.king_pawn_shield = !shield;
                const auto interrupted = reference_search(board, {.depth = 3, .nodes = 9}, nullptr, {}, options, &context);
                expect(interrupted.stopped && interrupted.nodes == 9,
                       "shelter mode switch ignored the shared cancellation budget");
                options.king_pawn_shield = shield;
                const auto restarted = reference_search(board, {.depth = 2}, nullptr, {}, options, &context);
                same_graph(fresh, restarted);
                expect(restarted.tt_reuses == 1 && !restarted.tt_allocations && board == saved,
                       "old shelter scores or interrupted work leaked through retained storage");
                valid_pv(board, restarted);
            }
            auto options = shelter_options(false, true, true);
            options.positional = false;
            const auto off = reference_search(board, {.depth = 2}, nullptr, {}, options, &context);
            options.king_pawn_shield = true;
            const auto on = reference_search(board, {.depth = 2}, nullptr, {}, options, &context);
            same_graph(off, on);
            expect(board == saved, "material-only shelter toggle changed input state/history");
        }
    });
    test("pawn structure ON and OFF match exhaustive ordinary and tactical search", [] {
        bool changed_value = false;
        std::uint64_t tactical_nodes = 0;
        for (const char* fen : {"7k/8/3p4/8/3P4/2P5/8/R5K1 w - - 0 1",
                                "7k/8/4p3/3r4/8/8/5PPP/3Q2K1 w - - 0 1",
                                "r5k1/8/2p5/3p4/8/3P4/8/7K b - - 0 1"}) {
            Board board(fen);
            const auto saved = board;
            for (bool shield : {false, true}) for (bool quiet : {false, true}) {
                const int depth = quiet ? 1 : 2;
                const auto previous = shelter_optimal_moves(board, depth, shield, quiet, false);
                const auto current = shelter_optimal_moves(board, depth, shield, quiet, true);
                changed_value |= previous.score != current.score;
                for (bool structure : {false, true}) for (bool table : {false, true}) {
                    const auto& expected = structure ? current : previous;
                    const auto result = reference_search(board, {.depth = depth}, nullptr, {},
                                               shelter_options(shield, quiet, table, structure));
                    expect(!result.stopped && result.depth == depth && result.score == expected.score,
                           "pawn option did not reach every ordinary/tactical frontier");
                    expect(result.best_move && std::find(expected.moves.begin(), expected.moves.end(),
                                                         *result.best_move) != expected.moves.end(),
                           "pawn search selected a move outside the exhaustive optimal set");
                    expect(quiet ? result.qnodes > 0 : result.qnodes == 0,
                           "pawn fixture did not exercise the selected frontier");
                    tactical_nodes += result.qnodes;
                    expect(board == saved, "pawn search or exhaustive oracle changed position/history");
                    valid_pv(board, result);
                }
            }
        }
        expect(changed_value && tactical_nodes > 0,
               "pawn search corpus must observe new scores and actual tactical work");
    });
    test("pawn structure defaults ON and remains selected in cancelled or one-node fallbacks", [] {
        Board board("7k/8/8/3P4/8/8/8/6K1 w - - 0 1");
        const auto saved = board;
        expect(SearchOptions{}.pawn_structure, "new search must enable pawn structure by default");
        expect(evaluate_position(board, true, true) != evaluate_position(board, true, false),
               "fallback fixture must distinguish pawn structure modes");
        const auto default_result = reference_search(board, {.depth = 2});
        auto explicit_on = SearchOptions{};
        explicit_on.pawn_structure = true;
        same_graph(default_result, reference_search(board, {.depth = 2}, nullptr, {}, explicit_on));
        for (bool structure : {false, true}) for (bool shield : {false, true}) for (bool quiet : {false, true}) {
            const auto options = shelter_options(shield, quiet, true, structure);
            std::atomic_bool stop{true};
            const auto stopped = reference_search(board, {.depth = 3}, &stop, {}, options);
            expect(stopped.stopped && stopped.depth == 0 && !stopped.nodes &&
                       stopped.score == evaluate_position(board, shield, structure),
                   "pre-stopped fallback lost the selected pawn structure mode");
            const auto limited = reference_search(board, {.depth = 3, .nodes = 1}, nullptr, {}, options);
            expect(limited.stopped && limited.depth == 0 && limited.nodes == 1 &&
                       limited.score == evaluate_position(board, shield, structure),
                   "one-node fallback lost the selected pawn structure mode");
            expect(board == saved, "pawn fallback changed position, clocks, hashes or history");
            valid_pv(board, stopped); valid_pv(board, limited);
        }
    });
    test("retained memory isolates pawn structure changes and material-only ignores both positional flags", [] {
        Board repeated;
        cycle(repeated); cycle(repeated);
        SearchContext context;
        for (const Board& board : {Board("7k/8/3p4/8/3P4/2P5/8/R5K1 w - - 0 1"), repeated}) {
            const auto saved = board;
            for (bool structure : {true, false, true}) for (bool quiet : {false, true}) {
                auto options = shelter_options(true, quiet, true, structure);
                const auto fresh = reference_search(board, {.depth = 2}, nullptr, {}, options);
                const auto reused = reference_search(board, {.depth = 2}, nullptr, {}, options, &context);
                same_graph(fresh, reused);
                options.pawn_structure = !structure;
                const auto interrupted = reference_search(board, {.depth = 3, .nodes = 9}, nullptr, {}, options, &context);
                expect(interrupted.stopped && interrupted.nodes == 9 && legal(board, interrupted.best_move),
                       "changing pawn modes bypassed cancellation or lost the legal fallback");
                options.pawn_structure = structure;
                const auto restarted = reference_search(board, {.depth = 2}, nullptr, {}, options, &context);
                same_graph(fresh, restarted);
                expect(restarted.tt_reuses == 1 && !restarted.tt_allocations && board == saved,
                       "old pawn values or interrupted work leaked through retained memory");
                valid_pv(board, restarted);
            }
            auto options = shelter_options(false, true, true, false);
            options.positional = false;
            const auto plain = reference_search(board, {.depth = 2}, nullptr, {}, options, &context);
            for (bool shield : {false, true}) for (bool structure : {false, true}) {
                options.king_pawn_shield = shield;
                options.pawn_structure = structure;
                same_graph(plain, reference_search(board, {.depth = 2}, nullptr, {}, options, &context));
            }
            expect(board == saved, "material-only positional toggles changed board/history");
        }
    });
    test("pawn bonuses never override mate automatic draws or optional claims", [] {
        for (bool structure : {false, true}) {
            const auto options = shelter_options(true, true, true, structure);
            Board mate("7k/q4Q2/6K1/8/3P4/8/8/8 w - - 149 1");
            const auto win = reference_search(mate, {.depth = 1}, nullptr, {}, options);
            expect(win.score == mate_score - 1 && legal(mate, win.best_move),
                   "pawn terms overrode mate distance or mate-before-75-move priority");
            mate.play(*win.best_move);
            expect(mate.outcome() == Outcome::checkmate, "pawn search's chosen mate does not mate");
            for (const char* fen : {"7k/5Q2/6K1/8/3P4/8/8/8 b - - 0 1",
                                    "7k/8/8/3P4/8/8/8/6K1 w - - 150 1"}) {
                const auto draw = reference_search(Board(fen), {.depth = 2}, nullptr, {}, options);
                expect(!draw.best_move && !draw.score && !draw.stopped,
                       "static pawn advantage escaped a terminal draw");
                no_memory_work(draw);
            }
            const Board losing("7k/8/8/3P4/8/8/q7/6K1 w - - 100 1");
            const auto claim = reference_search(losing, {.depth = 2}, nullptr, {}, options);
            expect(claim.score == 0 && legal(losing, claim.best_move),
                   "new pawn score suppressed a legal optional draw claim");
            valid_pv(losing, claim);
        }
    });
    test("LMR OFF retains full-depth reference graphs and shallow ON never reduces", [] {
        expect(SearchOptions{}.lmr, "LMR must be enabled in the requested default configuration");
        for (const char* fen : {"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                                lmr_quiet_fixture,
                                "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
                                "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
                                "4k3/P7/8/8/8/8/7p/4K3 w - - 0 1"}) {
            const Board board(fen);
            const auto saved = board;
            for (bool table : {false, true}) {
                const auto options = lmr_options(true, table);
                const auto previous = reference_search(board, {.depth = 4}, nullptr, {}, options);
                const auto enabled = search(board, {.depth = 4}, nullptr, {}, options);
                same_graph(previous, enabled);
                expect(!enabled.stopped && enabled.depth == 4 &&
                           !enabled.lmr_reductions && !enabled.lmr_researches,
                       "root protection and minimum remaining depth must disable all shallow reductions");
                expect(board == saved, "shallow LMR mode changed board, keys or repetition history");
                valid_pv(board, enabled);
            }
        }
    });
    test("LMR requires both PVS and learned quiet ordering and never reduces pawn or king moves", [] {
        const Board board(lmr_quiet_fixture);
        const auto saved = board;
        for (bool disable_pvs : {false, true}) {
            auto options = lmr_options(true);
            options.pvs = !disable_pvs;
            options.quiet_ordering = disable_pvs;
            const auto previous = reference_search(board, {.depth = 5}, nullptr, {}, options);
            const auto enabled = search(board, {.depth = 5}, nullptr, {}, options);
            same_graph(previous, enabled);
            expect(!enabled.stopped && enabled.depth == 5 && !enabled.lmr_reductions &&
                       !enabled.lmr_researches,
                   "PVS OFF or quiet ordering OFF must make LMR an exact no-op");
            valid_pv(board, enabled);
        }
        for (const char* fen : {"7k/8/8/3P4/8/8/8/6K1 w - - 0 1",
                                "6k1/8/8/8/3p4/8/8/7K b - - 0 1"}) {
            const Board ending(fen);
            const auto original = ending;
            const auto enabled = search(ending, {.depth = 5}, nullptr, {}, lmr_options(true));
            const auto previous = reference_search(ending, {.depth = 5}, nullptr, {}, lmr_options(false));
            same_graph(previous, enabled);
            expect(!enabled.stopped && !enabled.lmr_reductions && !enabled.lmr_researches,
                   "pawn pushes, king moves and king-pawn endings must remain full depth");
            expect(ending == original, "protected king-pawn search changed input state/history");
            valid_pv(ending, enabled);
        }
        expect(board == saved, "disabled-LMR gate checks changed board/history");
    });
    test("LMR really reduces and verifies late quiet scouts while publishing legal deterministic lines", [] {
        const Board board(lmr_quiet_fixture);
        const auto saved = board;
        int callbacks = 0;
        std::uint64_t reductions = 0, researches = 0, nodes = 0;
        const auto options = lmr_options(true);
        const auto result = search(board, {.depth = 5}, nullptr, [&](const SearchResult& iteration) {
            ++callbacks;
            expect(iteration.depth == callbacks && iteration.nodes > nodes && !iteration.stopped,
                   "LMR must publish complete iterations in order");
            expect(iteration.lmr_reductions >= reductions && iteration.lmr_researches >= researches,
                   "LMR work counters decreased across completed iterations");
            if (iteration.depth <= 4)
                expect(!iteration.lmr_reductions && !iteration.lmr_researches,
                       "LMR ran before a non-root depth-four node could exist");
            reductions = iteration.lmr_reductions;
            researches = iteration.lmr_researches;
            nodes = iteration.nodes;
            lmr_counts(iteration);
            valid_pv(board, iteration);
        }, options);
        expect(!result.stopped && result.depth == 5 && callbacks == 5,
               "deterministic LMR fixture failed to complete depth five");
        expect(result.lmr_reductions > 0 && result.lmr_researches > 0,
               "fixture must exercise actual late reductions and normal-depth verification");
        const auto repeated = search(board, {.depth = 5}, nullptr, {}, options);
        same_graph(result, repeated);
        const auto previous = reference_search(board, {.depth = 5}, nullptr, {}, options);
        expect(!previous.lmr_reductions && !previous.lmr_researches,
               "LMR OFF performed selective work");
        // Selective search can change a same-depth score or tied move. Verify
        // each legal PV, rather than requiring equivalence to full minimax.
        valid_pv(board, previous); valid_pv(board, result);
        expect(board == saved, "LMR selected or restored the wrong input position/history");
    });
    test("LMR interruption retains the last full iteration and its reusable memory stays isolated", [] {
        const Board board(lmr_quiet_fixture);
        const auto saved = board;
        const auto options = lmr_options(true);
        const auto shallow = search(board, {.depth = 4}, nullptr, {}, options);
        const auto complete = search(board, {.depth = 5}, nullptr, {}, options);
        expect(complete.lmr_reductions > 0 && complete.nodes > shallow.nodes + 1,
               "cancellation fixture must enter selective work after a complete shallow iteration");
        SearchContext context;
        const auto interrupted = search(board, {.depth = 5, .nodes = complete.nodes - 1},
                                        nullptr, {}, options, &context);
        expect(interrupted.stopped && interrupted.nodes == complete.nodes - 1,
               "LMR must share the exact ordinary node budget");
        same_completed(interrupted, shallow);
        expect(interrupted.lmr_reductions > 0,
               "cancelled iteration must actually include reduced search work");
        lmr_counts(interrupted); valid_pv(board, interrupted);
        auto previous_options = options;
        previous_options.lmr = false;
        const auto fresh_off = search(board, {.depth = 5}, nullptr, {}, previous_options);
        const auto reused_off = search(board, {.depth = 5}, nullptr, {}, previous_options, &context);
        same_graph(fresh_off, reused_off);
        const auto restarted = search(board, {.depth = 5}, nullptr, {}, options, &context);
        same_graph(complete, restarted);
        expect(restarted.tt_reuses == 1 && !restarted.tt_allocations,
               "LMR toggles should reuse storage while discarding previous bounds and reduced work");
        // An unverified reduced fail-low must never become a published best
        // line or a stored TT bound: mixed horizons do not prove a shallower
        // bound either. Restarting after partial work must reproduce a fresh
        // search, including its legal PV.
        valid_pv(board, restarted); valid_pv(board, reused_off);
        std::atomic_bool stop{true};
        const auto stopped = search(board, {.depth = 5}, &stop, {}, options, &context);
        expect(stopped.stopped && !stopped.nodes && !stopped.lmr_reductions && !stopped.lmr_researches,
               "preexisting stop must avoid LMR work and preserve a legal fallback");
        valid_pv(board, stopped);
        expect(board == saved, "LMR cancellation or option changes damaged board/history");
    });
    test("LMR protects current and intended draw claims before reducing any late knight move", [] {
        for (int clock : {98, 99, 100}) {
            const Board board("6nk/8/8/8/8/8/8/KN6 w - - " + std::to_string(clock) + " 51");
            const auto saved = board;
            const auto options = lmr_options(true, false);
            const auto previous = reference_search(board, {.depth = 5}, nullptr, {}, options);
            const auto enabled = search(board, {.depth = 5}, nullptr, {}, options);
            same_graph(previous, enabled);
            expect(!enabled.stopped && !enabled.lmr_reductions && !enabled.lmr_researches,
                   "current or intended fifty-move claims at eligible depth must forbid reductions");
            expect(board == saved, "LMR draw-claim protection changed clocks or repetition state");
            valid_pv(board, enabled);
        }
    });
    test("LMR preserves quiet checking mates sacrifices terminal results and optional claims", [] {
        for (const auto& [fen, expected] : std::array<std::pair<const char*, int>, 2>{{
                 {"7k/8/2b5/8/8/5q2/r4P1P/R5K1 w - - 0 1", -mate_score + 2},
                 {"5rnk/6pp/7N/3Q4/8/8/8/K7 w - - 0 1", mate_score - 3}}}) {
            const Board board(fen);
            const auto saved = board;
            auto options = lmr_options(true, false);
            options.quiescence = true;
            options.quiescence_checks = true;
            const auto result = search(board, {.depth = 5}, nullptr, {}, options);
            expect(!result.stopped && result.depth == 5 && result.score == expected,
                   "LMR lost the protected quiet checking mate or forced sacrifice");
            valid_pv(board, result); lmr_counts(result);
            Board line = board;
            for (Move move : result.pv) line.play(move);
            expect(line.outcome() == Outcome::checkmate,
                   "LMR's reported mate line does not reach checkmate on the actual board");
            expect(board == saved, "LMR mate regression changed the original position/history");
        }
        for (const char* fen : {"7k/6Q1/6K1/8/8/8/8/8 b - - 150 1",
                                "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1",
                                "7k/8/8/3P4/8/8/8/6K1 w - - 150 1"}) {
            const Board board(fen);
            const auto enabled = search(board, {.depth = 5}, nullptr, {}, lmr_options(true));
            const auto previous = reference_search(board, {.depth = 5}, nullptr, {}, lmr_options(false));
            same_graph(previous, enabled);
            expect(!enabled.best_move && !enabled.lmr_reductions && !enabled.lmr_researches,
                   "terminal mate/draw must precede selective work and return no move");
            no_memory_work(enabled);
        }
        const Board losing("7k/8/8/3P4/8/8/q7/6K1 w - - 100 1");
        const auto claim = search(losing, {.depth = 5}, nullptr, {}, lmr_options(true));
        expect(claim.score == 0 && legal(losing, claim.best_move),
               "LMR suppressed an available optional draw claim");
        valid_pv(losing, claim);
    });
    test("root restriction preserves every reply and the unrestricted default graph", [] {
        const Board board;
        const auto saved = board;
        SearchOptions options;
        options.lmr = false;
        const auto unrestricted = search(board, {.depth = 3}, nullptr, {}, options);
        options.root_moves = board.legal_moves();
        const auto every_root = search(board, {.depth = 3}, nullptr, {}, options);
        expect(unrestricted.best_move == every_root.best_move &&
                   unrestricted.score == every_root.score && unrestricted.pv == every_root.pv,
               "enumerating all root moves changed the position value or principal variation");
        options.root_moves = std::vector<Move>{Move::from_uci("a2a3")};
        const auto forced = search(board, {.depth = 3}, nullptr, {}, options);
        expect(forced.best_move == Move::from_uci("a2a3") && forced.pv.size() >= 3,
               "forced root did not retain normal descendant search");
        valid_pv(board, forced);
        SearchContext context;
        (void)search(board, {.depth = 3}, nullptr, {}, options, &context);
        options.root_moves.reset();
        const auto reused = search(board, {.depth = 3}, nullptr, {}, options, &context);
        same_graph(unrestricted, reused);
        expect(board == saved, "restricted search changed board or repetition history");
    });
    test("root restriction rejects empty duplicate and illegal moves", [] {
        const Board board;
        SearchOptions options;
        options.root_moves = std::vector<Move>{};
        rejects([&] { (void)search(board, {.depth = 2}, nullptr, {}, options); },
                "empty root restriction must fail closed");
        options.root_moves = std::vector<Move>{Move::from_uci("e2e5")};
        rejects([&] { (void)search(board, {.depth = 2}, nullptr, {}, options); },
                "illegal root move must fail closed");
        options.root_moves = std::vector<Move>{Move::from_uci("e2e4"), Move::from_uci("e2e4")};
        rejects([&] { (void)search(board, {.depth = 2}, nullptr, {}, options); },
                "duplicate root move must fail closed");
    });
    test("restricted fallback cancellation and every completed PV stay inside the list", [] {
        const Board board;
        SearchOptions options;
        options.root_moves = std::vector<Move>{Move::from_uci("h2h3"), Move::from_uci("a2a3")};
        std::atomic_bool stop{true};
        for (const auto& result : {search(board, {.depth = 4}, &stop, {}, options),
                                   search(board, {.depth = 4, .nodes = 1}, nullptr, {}, options)}) {
            expect(result.stopped && result.depth == 0 && result.best_move &&
                       std::find(options.root_moves->begin(), options.root_moves->end(), *result.best_move) !=
                           options.root_moves->end(), "fallback escaped root restriction");
            valid_pv(board, result);
        }
        int callbacks = 0;
        (void)search(board, {.depth = 3}, nullptr, [&](const SearchResult& result) {
            ++callbacks;
            expect(std::find(options.root_moves->begin(), options.root_moves->end(), *result.best_move) !=
                       options.root_moves->end(), "completed iteration escaped root restriction");
            valid_pv(board, result);
        }, options);
        expect(callbacks == 3, "restricted search omitted completed iterations");
    });
    test("root restriction handles castling promotion and en passant", [] {
        for (const auto& [fen, uci] : std::array<std::pair<const char*, const char*>, 3>{{
                 {"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"},
                 {"7k/P7/8/8/8/8/8/6K1 w - - 0 1", "a7a8n"},
                 {"7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1", "e5d6"}}}) {
            const Board board(fen);
            SearchOptions options;
            options.root_moves = std::vector<Move>{Move::from_uci(uci)};
            const auto result = search(board, {.depth = 2}, nullptr, {}, options);
            expect(result.best_move == Move::from_uci(uci), "special forced move was lost");
            valid_pv(board, result);
        }
    });
    test("root restriction preserves current draw claims and automatic adjudication", [] {
        const Board automatic("7k/8/8/3P4/8/8/q7/6K1 w - - 150 1");
        SearchOptions options;
        options.root_moves = std::vector<Move>{automatic.legal_moves().front()};
        const auto terminal = search(automatic, {.depth = 3}, nullptr, {}, options);
        expect(!terminal.best_move && terminal.score == 0 && !terminal.nodes,
               "root restriction bypassed automatic draw adjudication");
        const Board claimable_board("7k/8/8/3P4/8/8/q7/6K1 w - - 100 1");
        options.root_moves = std::vector<Move>{claimable_board.legal_moves().front()};
        const auto claim = search(claimable_board, {.depth = 3}, nullptr, {}, options);
        expect(claim.score == 0 && claim.best_move == options.root_moves->front(),
               "root restriction suppressed a current optional claim");
        valid_pv(claimable_board, claim);
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
