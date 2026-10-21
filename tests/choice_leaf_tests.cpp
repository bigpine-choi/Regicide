#include "../scripts/choice_leaf_support.hpp"

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
SearchOptions collection_options(Move move) {
    SearchOptions options;
    options.nnue = true; options.transposition = false; options.pvs = false; options.lmr = false;
    options.root_moves = std::vector<Move>{move};
    return options;
}
struct Collected {
    SearchOptions options;
    SearchResult result;
    std::vector<research::EvaluationWitness> observations;
    research::SupportDecision support;
};
Collected collect(const Board& root, Move forced, int depth = 3, std::uint64_t nodes = 10000) {
    Collected collected;
    collected.options = collection_options(forced);
    auto search_options = collected.options;
    search_options.static_evaluation_observer = [&](const Board& board, const std::vector<Move>& path,
                                                   const StaticEvaluationObservation& observation) {
        collected.observations.push_back(research::witness(board, path, observation));
    };
    collected.result = search(root, {.depth = depth, .nodes = nodes}, nullptr, {}, search_options);
    collected.support = research::support_leaf(root, forced, depth, collected.options, collected.result,
                                             collected.observations, builtin_neural_model());
    return collected;
}
void verify_support(const Board& root, Move forced) {
    const Board saved = root;
    const auto collected = collect(root, forced);
    expect(collected.support.accepted, "fixture rejected: " + collected.support.reason);
    expect(root == saved, "root/history changed");
    const auto& item = collected.observations.at(collected.support.witness_index);
    expect(item.path == collected.result.pv, "witness path does not equal completed PV");
    expect(item.observation.iteration_depth == 3, "witness from wrong iteration");
    Board leaf = root;
    for (auto move : item.path) leaf.play(move);
    expect(leaf.fen() == item.fen && leaf.history_size() == item.history_size, "full history replay mismatch");
    expect(leaf.repetition_context_key() == item.repetition_context, "repetition context mismatch");
    const auto raw = evaluate_neural(leaf);
    const int white = leaf.position().turn == Color::white ? raw : -raw;
    const int root_white = root.position().turn == Color::white ? collected.result.score : -collected.result.score;
    expect(white == root_white && white == collected.support.root_white_score, "root backup differs from raw leaf");
    expect(collected.result.tt_probes == 0 && collected.result.pvs_probes == 0 &&
           collected.result.lmr_reductions == 0, "collector used nonexact shortcut");
    expect(collected.observations.size() <= collected.result.nodes, "unbounded observer records");
}
} // namespace

int main() {
    test("completed forced root is backed by an actual final-iteration static witness", [] {
        verify_support(Board{}, Move::from_uci("e2e4"));
        Board history;
        for (const auto move : {"e2e4", "e7e5", "g1f3"}) history.play(move);
        verify_support(history, Move::from_uci("b8c6"));
    });
    test("special moves retain legal full history and signed backup", [] {
        verify_support(Board{"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"}, Move::from_uci("e1g1"));
        verify_support(Board{"7k/8/8/3pP3/8/8/8/K7 w - d6 0 1"}, Move::from_uci("e5d6"));
        verify_support(Board{"7k/P7/8/8/8/8/8/K7 w - - 0 1"}, Move::from_uci("a7a8q"));
    });
    test("fallback unfinished work and TT-shortened PV cannot masquerade as support", [] {
        const Board root;
        const auto forced = Move::from_uci("e2e4");
        auto collected = collect(root, forced);
        const auto assess = [&] { return research::support_leaf(root, forced, 3, collected.options,
            collected.result, collected.observations, builtin_neural_model()); };
        collected.result.stopped = true;
        expect(assess().reason == "incomplete_requested_depth", "stopped result accepted");
        collected.result.stopped = false;
        collected.result.depth = 2;
        expect(assess().reason == "incomplete_requested_depth", "previous completed depth accepted");
        collected.result.depth = 3;
        collected.options.transposition = true;
        expect(assess().reason == "unsupported_search_options", "TT collection accepted");
        collected.options.transposition = false;
        collected.result.pv.resize(1);
        expect(assess().reason == "no_final_iteration_pv_static_witness", "short PV without observation accepted");
        const auto interrupted = collect(root, forced, 3, 1);
        expect(!interrupted.support.accepted && interrupted.result.stopped, "actual node-limited fallback accepted");
    });
    test("matching a PV requires its completed iteration raw value and full replay", [] {
        const Board root;
        const auto forced = Move::from_uci("e2e4");
        auto collected = collect(root, forced);
        const auto original = collected;
        const auto assess = [&] { return research::support_leaf(root, forced, 3, collected.options,
            collected.result, collected.observations, builtin_neural_model()); };
        ++collected.result.score;
        expect(assess().reason == "backed_score_mismatch", "wrong root backup accepted");
        collected = original;
        auto& item = collected.observations.at(collected.support.witness_index);
        item.observation.iteration_depth = 2;
        expect(assess().reason == "no_final_iteration_pv_static_witness", "old iteration witness accepted");
        collected = original;
        ++collected.observations.at(collected.support.witness_index).history_size;
        expect(assess().reason == "witness_replay_mismatch", "truncated history accepted");
        collected = original;
        ++collected.observations.at(collected.support.witness_index).observation.score;
        expect(assess().reason == "clamped_static_witness", "clamped NNUE label accepted");
        collected = original;
        collected.observations.at(collected.support.witness_index).observation.draw_claim_adjusted = true;
        expect(assess().reason == "ineligible_static_witness", "draw-adjusted NNUE label accepted");
    });
    test("mate terminal claim and checked endpoints are excluded", [] {
        const auto forced = Move::from_uci("e2e4");
        auto options = collection_options(forced);
        SearchResult fake; fake.depth = 3; fake.best_move = forced; fake.pv = {forced};
        const auto assess = [&](const Board& board, Move move) {
            options.root_moves = std::vector<Move>{move};
            fake.best_move = move; fake.pv = {move};
            return research::support_leaf(board, move, 3, options, fake, {}, builtin_neural_model());
        };
        expect(assess(Board{"7k/6Q1/6K1/8/8/8/8/8 b - - 0 1"}, forced).reason == "terminal_root",
               "terminal root accepted");
        expect(assess(Board{"7k/8/8/3P4/8/8/q7/6K1 b - - 100 1"}, Move::from_uci("a2a3")).reason == "root_draw_claim",
               "claimable root accepted");
        expect(assess(Board{"7k/8/8/3P4/8/8/q7/6K1 b - - 99 1"}, Move::from_uci("a2a3")).reason == "pv_draw_claim",
               "intended draw claim along PV accepted");
        expect(assess(Board{"7k/8/6Q1/8/8/8/8/K7 w - - 0 1"}, Move::from_uci("g6h6")).reason == "checked_pv_endpoint",
               "checked endpoint accepted");
        fake.score = mate_score - 2;
        expect(assess(Board{}, forced).reason == "mate_band_root_score", "mate-band score accepted");
    });
    test("the new collector leaves existing default fixed-node traversal identical", [] {
        for (const auto& fen : {std::string(Board::start_fen), std::string("3q3k/8/8/3p4/4P3/8/8/3Q3K w - - 0 1"),
                               std::string("r3k2r/ppp2ppp/2n5/3pP3/8/8/PPP2PPP/R3K2R w KQkq d6 0 15")}) {
            const Board root(fen);
            for (bool incremental : {false, true}) {
                SearchOptions off; off.nnue = true; off.hash_mb = 1; off.nnue_incremental = incremental;
                const auto expected = search(root, {.depth = 5, .nodes = 600}, nullptr, {}, off);
                auto on = off;
                std::vector<research::EvaluationWitness> records;
                on.static_evaluation_observer = [&](const Board& board, const std::vector<Move>& path,
                                                    const StaticEvaluationObservation& observation) {
                    records.push_back(research::witness(board, path, observation));
                };
                const auto actual = search(root, {.depth = 5, .nodes = 600}, nullptr, {}, on);
                expect(identity(actual) == identity(expected), "default search behavior/counters changed");
                expect(!records.empty() && records.size() <= actual.nodes, "invalid observer count");
            }
        }
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
