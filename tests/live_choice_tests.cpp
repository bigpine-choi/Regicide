#include "../scripts/live_choice_support.hpp"

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
struct Collected {
    SearchOptions options;
    SearchResult final_result, completed;
    std::size_t completed_observations = 0;
    std::vector<research::EvaluationWitness> observations;
    research::SupportDecision support;
};
research::SupportDecision assess(const Board& root, const Collected& c, int depth = 2) {
    return research::live_support_leaf(root, depth, c.options, c.final_result, c.completed,
        c.completed_observations, c.observations, builtin_neural_model());
}
Collected collect(const Board& root, std::optional<Move> forced, int depth = 2, std::uint64_t nodes = 10000) {
    Collected c;
    c.options.nnue = true;
    if (forced) c.options.root_moves = std::vector<Move>{*forced};
    auto options = c.options;
    options.static_evaluation_observer = [&](const Board& board, const std::vector<Move>& path,
                                            const StaticEvaluationObservation& observation) {
        c.observations.push_back(research::witness(board, path, observation));
    };
    c.final_result = search(root, {.depth = depth, .nodes = nodes}, nullptr,
        [&](const SearchResult& iteration) {
            c.completed = iteration;
            c.completed_observations = c.observations.size();
        }, options);
    c.support = assess(root, c, depth);
    return c;
}
void verify_support(const Board& root, Move move) {
    const auto saved = root;
    const auto c = collect(root, move);
    expect(c.support.accepted, "fixture rejected: " + c.support.reason);
    expect(root == saved, "root/history mutated");
    const auto& item = c.observations.at(c.support.witness_index);
    expect(c.support.witness_index < c.completed_observations, "tail witness selected");
    expect(item.path == c.completed.pv && item.observation.iteration_depth == c.completed.depth,
           "completed path/depth mismatch");
    Board leaf = root;
    for (const auto step : item.path) leaf.play(step);
    expect(leaf.fen() == item.fen && leaf.history_size() == item.history_size &&
           leaf.repetition_context_key() == item.repetition_context, "history replay mismatch");
    const int raw = evaluate_neural(leaf);
    const int white = leaf.position().turn == Color::white ? raw : -raw;
    expect(white == c.support.root_white_score, "signed white backup mismatch");
    expect(c.options.transposition && c.options.pvs && c.options.lmr && c.completed.tt_probes > 0,
           "default search flags not exercised");
}
} // namespace

int main() {
    test("default forced support preserves white black and full histories", [] {
        verify_support(Board{}, Move::from_uci("e2e4"));
        Board black;
        for (const auto step : {"e2e4", "e7e5", "g1f3"}) black.play(step);
        verify_support(black, Move::from_uci("b8c6"));
    });
    test("castling en passant and promotion have replayable default support", [] {
        verify_support(Board{"r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"}, Move::from_uci("e1g1"));
        verify_support(Board{"7k/8/8/3pP3/8/8/8/K7 w - d6 0 1"}, Move::from_uci("e5d6"));
        verify_support(Board{"7k/P7/8/8/8/8/8/K7 w - - 0 1"}, Move::from_uci("a7a8q"));
    });
    test("node-cap tail cannot replace the last completed callback witness", [] {
        const Board root;
        auto c = collect(root, Move::from_uci("e2e4"));
        expect(c.support.accepted, "base fixture rejected");
        c.final_result.stopped = true;
        ++c.final_result.nodes;
        c.observations.push_back(c.observations.at(c.support.witness_index));
        c.observations.back().observation.iteration_depth = 3;
        expect(assess(root, c).accepted, "valid completed prefix lost due to incomplete tail");
        for (std::size_t i = 0; i < c.completed_observations; ++i)
            if (c.observations[i].path == c.completed.pv)
                c.observations[i].observation.iteration_depth = 1;
        c.observations.back().observation.iteration_depth = 2;
        expect(assess(root, c).reason == "no_completed_prefix_pv_static_witness", "tail used as completed support");
        auto unfinished = collect(root, Move::from_uci("e2e4"), 64, 1);
        expect(unfinished.final_result.stopped && !unfinished.support.accepted,
               "actual fallback without callback accepted");
    });
    test("TT-shortened completed PV wrong value and history are rejected", [] {
        const Board root;
        auto c = collect(root, Move::from_uci("e2e4"));
        const auto original = c;
        c.completed.pv.resize(1); c.final_result.pv = c.completed.pv;
        expect(assess(root, c).reason == "truncated_completed_pv", "TT truncated PV accepted");
        c = original; ++c.completed.score; c.final_result.score = c.completed.score;
        expect(assess(root, c).reason == "backed_score_mismatch", "wrong numeric support accepted");
        c = original; ++c.observations.at(c.support.witness_index).history_size;
        expect(assess(root, c).reason == "witness_replay_mismatch", "truncated history accepted");
        c = original; c.completed_observations = c.observations.size()+1;
        expect(assess(root, c).reason == "missing_or_inconsistent_completed_iteration", "invalid prefix accepted");
        c = original; c.options.transposition = false;
        expect(assess(root, c).reason == "unsupported_default_search_options", "non-default profile accepted");
        c = original; ++c.observations.at(c.support.witness_index).observation.score;
        expect(assess(root, c).reason == "clamped_static_witness", "clamped witness accepted");
        c = original; c.observations.at(c.support.witness_index).observation.draw_claim_adjusted = true;
        expect(assess(root, c).reason == "ineligible_static_witness", "draw-adjusted witness accepted");
    });
    test("mate draw terminal and checked endpoints cannot become support", [] {
        const Board root;
        auto c = collect(root, Move::from_uci("e2e4"));
        c.completed.score = mate_score-2; c.final_result.score = c.completed.score;
        expect(assess(root, c).reason == "mate_band_root_score", "mate-band accepted");
        const Board terminal{"7k/6Q1/6K1/8/8/8/8/8 b - - 0 1"};
        expect(assess(terminal, c).reason == "terminal_root", "terminal accepted");
        const Board claim{"7k/8/8/3P4/8/8/q7/6K1 b - - 100 1"};
        expect(assess(claim, c).reason == "root_draw_claim", "claimable root accepted");
        const Board checked{"7k/8/6Q1/8/8/8/8/K7 w - - 0 1"};
        c.options.root_moves = std::vector<Move>{Move::from_uci("g6h6")};
        c.completed.depth = 1; c.completed.score = 0; c.completed.best_move = c.options.root_moves->front();
        c.completed.pv = {*c.completed.best_move}; c.final_result = c.completed;
        expect(assess(checked, c).reason == "checked_pv_endpoint", "checked endpoint accepted");
    });
    test("callback and observer retain default unrestricted traversal exactly", [] {
        for (const auto& fen : {std::string(Board::start_fen), std::string("3q3k/8/8/3p4/4P3/8/8/3Q3K w - - 0 1"),
                               std::string("r3k2r/ppp2ppp/2n5/3pP3/8/8/PPP2PPP/R3K2R w KQkq d6 0 15")}) {
            const Board root(fen);
            for (bool incremental : {false, true}) {
                SearchOptions off; off.nnue = true; off.nnue_incremental = incremental;
                const auto expected = search(root, {.depth = 64, .nodes = 600}, nullptr, {}, off);
                std::vector<research::EvaluationWitness> records;
                auto on = off;
                on.static_evaluation_observer = [&](const Board& board, const std::vector<Move>& path,
                                                    const StaticEvaluationObservation& observation) {
                    records.push_back(research::witness(board, path, observation));
                };
                SearchResult completed;
                std::size_t prefix = 0;
                const auto actual = search(root, {.depth = 64, .nodes = 600}, nullptr,
                    [&](const SearchResult& iteration) { completed = iteration; prefix = records.size(); }, on);
                expect(identity(actual) == identity(expected), "observer/callback changed traversal");
                expect(!records.empty() && records.size() <= actual.nodes && prefix <= records.size(),
                       "invalid bounded observation prefix");
                expect(completed.depth == actual.depth && completed.pv == actual.pv && completed.score == actual.score,
                       "unfinished tail overwrote completed callback result");
            }
        }
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
