#include "regicide/search.hpp"

#include <algorithm>
#include <atomic>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace regicide;
namespace {
int failures = 0, groups = 0;
void expect(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}
void test(const char* name, const std::function<void()>& action) {
    ++groups;
    try { action(); std::cout << "PASS " << name << '\n'; }
    catch (const std::exception& error) {
        ++failures;
        std::cerr << "FAIL " << name << ": " << error.what() << '\n';
    }
}
void valid_pv(const Board& original, const SearchResult& result) {
    Board board = original;
    expect(result.best_move && !result.pv.empty() && result.pv.front() == *result.best_move,
           "missing or mismatched legal root PV");
    for (Move move : result.pv) {
        const auto legal = board.legal_moves();
        expect(board.outcome() == Outcome::ongoing &&
               std::find(legal.begin(), legal.end(), move) != legal.end(), "illegal PV");
        board.play(move);
    }
}
SearchOptions options(bool enabled = true) {
    SearchOptions value;
    value.quiescence_forcing = enabled;
    value.transposition = false;
    value.pvs = false;
    value.lmr = false;
    value.hash_mb = 1;
    value.piece_activity = true;
    return value;
}
void same_graph(const SearchResult& a, const SearchResult& b) {
    expect(a.score == b.score && a.depth == b.depth && a.best_move == b.best_move &&
           a.pv == b.pv && a.nodes == b.nodes && a.qnodes == b.qnodes &&
           a.seldepth == b.seldepth && a.stopped == b.stopped &&
           a.forcing_checks == b.forcing_checks,
           "identical search semantics changed results or traversal");
}
Board escape_root() {
    // The disclosed development history, not a private game. Restrict only the
    // root to Rg7+; the checked king retains every legal escape in the search.
    Board board("r4k2/2p5/1pqp4/5r2/p3R2R/2B3P1/PP3P1P/6K1 b - - 3 31");
    for (const char* move : {"a4a3", "h4h8", "f8f7", "h8h7", "f7g6"}) board.play(move);
    return board;
}
bool prefix(const std::vector<Move>& path, const std::vector<Move>& wanted) {
    return path.size() >= wanted.size() && std::equal(wanted.begin(), wanted.end(), path.begin());
}
SearchOptions escape_options(bool enabled) {
    auto value = options(enabled);
    value.root_moves = std::vector<Move>{Move::from_uci("h7g7")};
    return value;
}
Board losing_king() {
    return Board("7k/8/8/8/8/8/r7/6K1 w - - 0 1");
}
void cycle(Board& board) {
    for (const char* move : {"g1f1", "h8g8", "f1g1", "g8h8"}) board.play(move);
}
} // namespace

int main() {
    test("default-off policy preserves the previous traversal", [] {
        for (const char* fen : {Board::start_fen.data(),
                 "7k/8/2b5/8/8/5q2/r4P1P/R5K1 w - - 0 1",
                 "r4k2/2p5/1pqp4/5r2/p3R2R/2B3P1/PP3P1P/6K1 b - - 3 31"}) {
            const Board root(fen), saved = root;
            SearchOptions implicit;
            implicit.hash_mb = 1;
            auto explicit_off = implicit;
            explicit_off.quiescence_forcing = false;
            const auto a = search(root, {.depth = 2, .nodes = 3000}, nullptr, {}, implicit);
            const auto b = search(root, {.depth = 2, .nodes = 3000}, nullptr, {}, explicit_off);
            same_graph(a, b);
            expect(!a.forcing_checks && root == saved, "disabled policy did work or changed board history");
            valid_pv(root, a);
        }
    });
    test("a disclosed king escape now reaches the second quiet checking continuation", [] {
        const Board root = escape_root(), saved = root;
        const std::vector<Move> wanted{
            Move::from_uci("h7g7"), Move::from_uci("g6h6"),
            Move::from_uci("e4e6"), Move::from_uci("h6h5"), Move::from_uci("g3g4")};
        bool previous_saw = false, candidate_saw = false;
        for (bool enabled : {false, true}) {
            auto selected = escape_options(enabled);
            selected.static_evaluation_observer = [&](const Board& leaf, const std::vector<Move>& path,
                                                       const StaticEvaluationObservation& observation) {
                Board replay = root;
                for (Move move : path) replay.play(move);
                expect(replay == leaf && path.size() == static_cast<std::size_t>(observation.ply),
                       "observer path does not reproduce the actual board and history");
                if (observation.qply >= 0) expect(!leaf.in_check(), "checked node used stand pat");
                if (prefix(path, wanted)) {
                    expect(observation.qply >= 4, "continuation appeared before the declared qsearch horizon");
                    (enabled ? candidate_saw : previous_saw) = true;
                }
            };
            const auto result = search(root, {.depth = 1, .nodes = 1000000}, nullptr, {}, selected);
            expect(result.depth == 1 && !result.stopped, "small disclosed continuation did not complete");
            expect(enabled ? result.forcing_checks > 0 : result.forcing_checks == 0,
                   "additional checking work was not identified correctly");
            expect(root == saved, "continuation changed the original board or repetition history");
            valid_pv(root, result);
        }
        expect(!previous_saw && candidate_saw, "new policy did not expose the previously omitted g4+ line");
    });
    test("qsearch cache preserves the completed continuation value and legal PV", [] {
        const Board root = escape_root(), saved = root;
        auto plain = escape_options(true), cached = plain;
        cached.quiescence_cache = true;
        const auto a = search(root, {.depth = 1, .nodes = 1000000}, nullptr, {}, plain);
        const auto b = search(root, {.depth = 1, .nodes = 1000000}, nullptr, {}, cached);
        expect(a.depth == 1 && b.depth == 1 && !a.stopped && !b.stopped &&
               a.score == b.score && a.best_move == b.best_move && a.forcing_checks && b.forcing_checks,
               "context-aware qsearch cache changed the completed tactical value");
        expect(b.qcache_probes && b.qcache_stores && root == saved, "cache coverage or board restoration missing");
        valid_pv(root, a);
        valid_pv(root, b);
    });
    test("late quiet checks spend the finite continuation allowance even after a checking capture", [] {
        // A synthetic variant of the disclosed position adds an e6 pawn.
        // Rg7+ Kh6 Rxe6+ Kh5 reaches qply 3 without spending the ordinary
        // quiet-check credit: the intervening check captured a pawn. With the
        // early-qply policy enabled, g4+ must use the separate finite allowance.
        const Board root("r7/2p4R/1pqpp1k1/5r2/4R3/p1B3P1/PP3P1P/6K1 w - - 4 34"), saved = root;
        const std::vector<Move> wanted{
            Move::from_uci("h7g7"), Move::from_uci("g6h6"),
            Move::from_uci("e4e6"), Move::from_uci("h6h5"), Move::from_uci("g3g4")};
        Board witness = root;
        for (std::size_t i = 0; i < wanted.size(); ++i) {
            if (i == 2) expect(witness.position().squares[wanted[i].to] == 'p',
                               "the preparation is no longer a checking capture");
            witness.play(wanted[i]);
            if (i == 2 || i == 4) expect(witness.in_check(), "declared tactical witness lost check");
        }
        auto selected = options();
        selected.quiescence_horizon_budget = true;
        selected.root_moves = std::vector<Move>{wanted.front()};
        bool saw_late_check = false;
        unsigned maximum_late_checks = 0;
        selected.static_evaluation_observer = [&](const Board& leaf, const std::vector<Move>& path,
                                                   const StaticEvaluationObservation&) {
            Board replay = root;
            unsigned late_checks = 0;
            for (std::size_t i = 0; i < path.size(); ++i) {
                const auto& position = replay.position();
                const char piece = position.squares[path[i].from];
                const bool ep = (piece == 'P' || piece == 'p') && path[i].to == position.en_passant;
                const bool quiet = position.squares[path[i].to] == '.' && !ep && !path[i].promotion;
                // One ordinary root ply precedes qsearch. At index 4 the
                // source node is qply 3, outside the ordinary quiet horizon.
                if (i >= 4 && !replay.in_check() && quiet && replay.gives_check_generated(path[i]))
                    ++late_checks;
                replay.play(path[i]);
            }
            expect(replay == leaf, "late-check observation lost the actual history");
            expect(late_checks <= 2, "a path exceeded the two additional nonmandatory late checks");
            maximum_late_checks = std::max(maximum_late_checks, late_checks);
            if (prefix(path, wanted)) saw_late_check = true;
        };
        const auto result = search(root, {.depth = 1, .nodes = 1000000}, nullptr, {}, selected);
        expect(result.depth == 1 && !result.stopped && result.forcing_checks && saw_late_check &&
               maximum_late_checks > 0 && root == saved,
               "late checking-capture witness did not exercise the bounded continuation");
        valid_pv(root, result);
    });
    test("a broad checked frontier keeps every escape and ends additional checking pursuit", [] {
        const Board root("8/8/4k3/8/8/8/8/R3K3 w - - 0 1"), saved = root;
        const Move checking_move = Move::from_uci("a1a6");
        Board checked = root;
        checked.play(checking_move);
        const auto escapes = checked.legal_moves();
        expect(checked.in_check() && escapes.size() > 2, "declared broad-defense frontier changed");
        std::vector<Move> observed_escapes;
        auto selected = options();
        selected.quiescence_checks = false;
        selected.root_moves = std::vector<Move>{checking_move};
        selected.static_evaluation_observer = [&](const Board&, const std::vector<Move>& path,
                                                   const StaticEvaluationObservation&) {
            if (path.size() == 2 && path.front() == checking_move &&
                std::find(observed_escapes.begin(), observed_escapes.end(), path[1]) == observed_escapes.end())
                observed_escapes.push_back(path[1]);
        };
        const auto candidate = search(root, {.depth = 1, .nodes = 100000}, nullptr, {}, selected);
        expect(candidate.depth == 1 && !candidate.stopped && !candidate.forcing_checks &&
               observed_escapes.size() == escapes.size() && root == saved,
               "broad-defense cutoff pruned an evasion or retained extra checking pursuit");
        selected.quiescence_forcing = false;
        selected.static_evaluation_observer = {};
        const auto previous = search(root, {.depth = 1, .nodes = 100000}, nullptr, {}, selected);
        same_graph(candidate, previous);
        valid_pv(root, candidate);
    });
    test("the established quiet mating refutation survives chain expansion", [] {
        const Board root("7k/8/2b5/8/8/5q2/r4P1P/R5K1 w - - 0 1"), saved = root;
        auto selected = options();
        selected.positional = false;
        const auto result = search(root, {.depth = 1, .nodes = 100000}, nullptr, {}, selected);
        expect(result.depth == 1 && !result.stopped && result.score == -mate_score + 2,
               "extra forcing work lost the established quiet mating refutation");
        valid_pv(root, result);
        Board leaf = root;
        for (Move move : result.pv) leaf.play(move);
        expect(leaf.outcome() == Outcome::checkmate && root == saved, "mating PV or original history changed");
    });
    test("a mandatory quiet countercheck survives even without ordinary quiet-check credits", [] {
        // Re8+ has exactly one legal reply: ...Bf8+, interposing and checking
        // the white king. Rxf8# then removes the bishop. This witness is
        // independently checked with the public legal-move/rule interface.
        const Board root("7k/8/7K/2b5/8/8/8/4R3 w - - 0 1"), saved = root;
        Board witness = root;
        witness.play("e1e8");
        expect(witness.in_check() && witness.legal_moves() == std::vector<Move>{Move::from_uci("c5f8")},
               "fixture no longer has its sole quiet checking interposition");
        witness.play("c5f8");
        expect(witness.in_check(), "interposition ceased to be a countercheck");
        witness.play("e8f8");
        expect(witness.outcome() == Outcome::checkmate, "independent counterchecking witness lost mate");
        for (bool enabled : {false, true}) {
            auto selected = options(enabled);
            selected.quiescence_checks = false;
            selected.root_moves = std::vector<Move>{Move::from_uci("e1e8")};
            const auto result = search(root, {.depth = 1, .nodes = 10000}, nullptr, {}, selected);
            expect(result.depth == 1 && !result.stopped && result.score == mate_score - 3 && root == saved,
                   "zero-credit check evasions or their mating counterattack were filtered");
            expect(result.pv == std::vector<Move>{Move::from_uci("e1e8"), Move::from_uci("c5f8"),
                                                Move::from_uci("e8f8")},
                   "counterchecking PV differs from the independently verified forced line");
            valid_pv(root, result);
        }
    });
    test("preexisting stop and exact node limits restore board and legal fallback", [] {
        const Board root = escape_root(), saved = root;
        auto selected = escape_options(true);
        selected.transposition = true;
        selected.quiescence_cache = true;
        SearchContext context;
        const std::atomic_bool stop{true};
        const auto stopped = search(root, {.depth = 3}, &stop, {}, selected, &context);
        expect(stopped.stopped && !stopped.nodes && !stopped.forcing_checks,
               "preexisting cancellation performed tactical work");
        valid_pv(root, stopped);
        for (std::uint64_t limit : {1U, 50U, 500U}) {
            const auto result = search(root, {.depth = 3, .nodes = limit}, nullptr, {}, selected, &context);
            expect(result.stopped && result.nodes == limit && root == saved,
                   "forcing recursion exceeded the exact cancellation budget or changed history");
            valid_pv(root, result);
        }
        auto off = selected;
        off.quiescence_forcing = false;
        const auto fresh = search(root, {.depth = 1}, nullptr, {}, off);
        const auto reused = search(root, {.depth = 1}, nullptr, {}, off, &context);
        same_graph(fresh, reused);
        expect(!reused.forcing_checks && root == saved, "stopped or enabled work leaked into a later search");
    });
    test("asynchronous cancellation during an observed tactical line retains legal state", [] {
        const Board root = escape_root(), saved = root;
        std::atomic_bool stop{false};
        auto selected = escape_options(true);
        std::uint64_t observed = 0;
        selected.static_evaluation_observer = [&](const Board&, const std::vector<Move>&,
                                                  const StaticEvaluationObservation&) {
            if (++observed == 20) stop.store(true);
        };
        const auto result = search(root, {.depth = 3, .nodes = 100000}, &stop, {}, selected);
        expect(observed >= 20 && result.stopped && root == saved,
               "observer-triggered cancellation did not unwind the tactical tree");
        valid_pv(root, result);
    });
    test("actual threefold history remains a draw resource instead of a cycle penalty", [] {
        Board root = losing_king();
        cycle(root); cycle(root);
        const Board saved = root, imported(root.fen());
        expect(root.claims_now().threefold && !imported.claims_now().threefold,
               "fixture no longer distinguishes actual repetition history from imported FEN");
        auto selected = options();
        selected.positional = false;
        selected.transposition = true;
        selected.quiescence_cache = true;
        SearchContext context;
        const auto draw = search(root, {.depth = 1, .nodes = 10000}, nullptr, {}, selected, &context);
        const auto losing = search(imported, {.depth = 1, .nodes = 10000}, nullptr, {}, selected, &context);
        expect(draw.depth == 1 && draw.score == 0 && losing.depth == 1 && losing.score < 0,
               "forcing policy erased a real draw claim or fabricated one in a new history");
        expect(root == saved, "claim search altered its actual history");
        valid_pv(root, draw);
        valid_pv(imported, losing);
    });
    test("intended fifty-move claim and automatic draws keep their rule priority", [] {
        const Board root("7k/8/8/8/8/8/r7/6K1 w - - 99 1"), saved = root;
        auto selected = options();
        selected.positional = false;
        const auto result = search(root, {.depth = 2, .nodes = 10000}, nullptr, {}, selected);
        expect(result.depth == 2 && result.score == 0 && root == saved,
               "quiet intended fifty-move claim was lost");
        valid_pv(root, result);
        Board fivefold = losing_king();
        for (int repetition = 0; repetition < 4; ++repetition) cycle(fivefold);
        expect(fivefold.outcome() == Outcome::fivefold_repetition, "fivefold fixture changed");
        for (const Board& terminal : {fivefold, Board("7k/8/8/8/8/8/r7/6K1 w - - 150 1")}) {
            const auto ended = search(terminal, {.depth = 3}, nullptr, {}, selected);
            expect(ended.score == 0 && !ended.nodes && !ended.forcing_checks && !ended.best_move,
                   "automatic draw performed forcing work or returned an artificial penalty");
        }
    });
    test("quiet checkmate outranks the seventy-five-move counter", [] {
        const Board root("7k/5Q2/6K1/8/8/8/8/8 w - - 149 1"), saved = root;
        auto selected = options();
        selected.root_moves = std::vector<Move>{Move::from_uci("f7f8")};
        const auto result = search(root, {.depth = 1}, nullptr, {}, selected);
        expect(result.depth == 1 && result.score == mate_score - 1 && root == saved,
               "forcing policy let the move counter outrank actual mate");
        valid_pv(root, result);
    });
    std::cout << groups - failures << '/' << groups << " forcing groups passed\n";
    return failures ? 1 : 0;
}
