#include "regicide/search.hpp"
#include "regicide/nnue.hpp"

#include <algorithm>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <tuple>
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
    catch (const std::exception& error) { ++failures; std::cerr << "FAIL " << name << ": " << error.what() << '\n'; }
}
auto identity(const SearchResult& r) {
    return std::tie(r.best_move, r.score, r.depth, r.nodes, r.qnodes, r.tt_probes, r.tt_hits,
        r.tt_cutoffs, r.tt_stores, r.tt_allocations, r.tt_reuses, r.tt_clears, r.tt_bytes,
        r.pvs_probes, r.pvs_researches, r.see_calls, r.lmr_reductions, r.lmr_researches,
        r.seldepth, r.stopped, r.pv);
}
void replay(const Board& root, const Board& leaf, const std::vector<Move>& path,
            const StaticEvaluationObservation& observation) {
    Board copy = root;
    for (auto move : path) copy.play(move);
    expect(copy == leaf, "observer history/path differs from actual board");
    expect(path.size() == static_cast<std::size_t>(observation.ply), "path/ply mismatch");
    expect(observation.raw_score == evaluate_neural(leaf), "incremental/full-refresh static mismatch");
    const int margin = observation.qply < 0 ? max_search_depth : max_search_ply;
    expect(observation.score == std::clamp(observation.raw_score, -mate_score + margin + 1,
                                          mate_score - margin - 1), "wrong score clamp");
    expect(leaf.outcome() == Outcome::ongoing, "terminal board observed");
    if (observation.qply >= 0) expect(!leaf.in_check(), "checked quiescence used stand pat");
}
} // namespace
int main() {
    test("observer preserves deterministic traversal across NNUE search switches", [] {
        const std::vector<std::string> fens{
            std::string(Board::start_fen), "3q3k/8/8/3p4/4P3/8/8/3Q3K w - - 0 1",
            "r3k2r/ppp2ppp/2n5/3pP3/8/8/PPP2PPP/R3K2R w KQkq d6 0 15"};
        for (const auto& fen : fens) for (bool quiet : {false, true})
            for (bool tt : {false, true}) for (bool pvs : {false, true})
                for (bool incremental : {false, true}) {
                    const Board root(fen);
                    SearchOptions off;
                    off.nnue = true; off.hash_mb = 1; off.quiescence = quiet;
                    off.transposition = tt; off.pvs = pvs; off.nnue_incremental = incremental;
                    SearchLimits limits; limits.depth = 5; limits.nodes = 600;
                    const auto expected = search(root, limits, nullptr, {}, off);
                    auto on = off;
                    std::uint64_t calls = 0;
                    on.static_evaluation_observer = [&](const Board& leaf, const std::vector<Move>& path,
                                                        const StaticEvaluationObservation& observation) {
                        expect(observation.evaluation_index == ++calls, "nonsequential invocation index");
                        expect(observation.iteration_depth >= 1, "missing active iteration");
                        if (calls <= 12) replay(root, leaf, path, observation);
                    };
                    const auto actual = search(root, limits, nullptr, {}, on);
                    expect(identity(actual) == identity(expected), "observer changed search decisions/counters");
                    expect(calls > 0, "no evaluator observations");
                }
    });
    test("observations include unfinished work without claiming a completed PV leaf", [] {
        Board root;
        root.play("e2e4"); root.play("e7e5");
        const Board saved = root;
        SearchOptions options; options.nnue = true; options.hash_mb = 1;
        std::vector<int> iterations;
        options.static_evaluation_observer = [&](const Board& leaf, const std::vector<Move>& path,
                                                const StaticEvaluationObservation& observation) {
            replay(root, leaf, path, observation);
            iterations.push_back(observation.iteration_depth);
        };
        SearchLimits limits; limits.depth = max_search_depth; limits.nodes = 100;
        const auto result = search(root, limits, nullptr, {}, options);
        expect(result.stopped && result.nodes == 100, "node limit not respected");
        expect(std::any_of(iterations.begin(), iterations.end(), [&](int d) { return d > result.depth; }),
               "interrupted-iteration observations were concealed");
        expect(root == saved, "root/history not restored after interrupted observation");
    });
    test("terminal and stopped fallback scores never masquerade as leaves", [] {
        SearchOptions options; options.nnue = true; options.hash_mb = 1;
        int calls = 0;
        options.static_evaluation_observer = [&](const Board&, const std::vector<Move>&,
                                                const StaticEvaluationObservation&) { ++calls; };
        SearchLimits limits; limits.depth = 3;
        const std::atomic_bool stopped{true};
        (void)search(Board{}, limits, &stopped, {}, options);
        for (const auto& fen : {"7k/6Q1/6K1/8/8/8/8/8 b - - 0 1",
                                "7k/8/8/8/8/8/8/K7 w - - 0 1",
                                "7k/8/8/3P4/8/8/q7/6K1 w - - 150 1"})
            (void)search(Board{fen}, limits, nullptr, {}, options);
        expect(calls == 0, "terminal/root fallback observation emitted");
    });
    test("draw-adjusted evaluations carry exclusion metadata", [] {
        const Board root("7k/8/8/3P4/8/8/q7/6K1 b - - 100 1");
        SearchOptions options; options.nnue = true; options.hash_mb = 1;
        options.quiescence = false;
        bool saw_current = false, saw_adjusted = false;
        options.static_evaluation_observer = [&](const Board& leaf, const std::vector<Move>& path,
                                                const StaticEvaluationObservation& observation) {
            replay(root, leaf, path, observation);
            saw_current |= observation.current_draw_claim;
            saw_adjusted |= observation.draw_claim_adjusted;
            expect(observation.current_draw_claim ==
                (leaf.position().halfmove >= 100 || leaf.repetition_count() >= 3), "claim metadata wrong");
            if (observation.draw_claim_adjusted) expect(observation.score < 0, "nonnegative score draw-adjusted");
        };
        (void)search(root, {.depth = 1}, nullptr, {}, options);
        expect(saw_current && saw_adjusted, "fixture did not exercise draw-adjusted evaluator calls");
    });
    test("special-move leaf paths replay complete repetition history", [] {
        for (const auto& fen : {"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
                                "7k/8/8/3pP3/8/8/8/K7 w - d6 0 1",
                                "7k/P7/8/8/8/8/8/K7 w - - 0 1"}) {
            const Board root(fen);
            SearchOptions options; options.nnue = true; options.hash_mb = 1;
            int calls = 0;
            options.static_evaluation_observer = [&](const Board& leaf, const std::vector<Move>& path,
                                                    const StaticEvaluationObservation& observation) {
                replay(root, leaf, path, observation); ++calls;
            };
            (void)search(root, {.depth = 2, .nodes = 500}, nullptr, {}, options);
            expect(calls > 0, "special fixture produced no evaluations");
        }
    });
    test("throwing observer restores input and search-local move path", [] {
        const Board root;
        const Board saved = root;
        SearchOptions options; options.nnue = true; options.hash_mb = 1;
        options.static_evaluation_observer = [](const Board&, const std::vector<Move>&,
                                               const StaticEvaluationObservation&) {
            throw std::runtime_error("intentional observer failure");
        };
        bool threw = false;
        try { (void)search(root, {.depth = 2}, nullptr, {}, options); }
        catch (const std::runtime_error&) { threw = true; }
        expect(threw && root == saved, "observer failure did not safely unwind");
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
