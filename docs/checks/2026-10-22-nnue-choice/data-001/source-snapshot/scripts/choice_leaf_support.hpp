#pragma once

#include "regicide/nnue.hpp"
#include "regicide/search.hpp"

#include <algorithm>
#include <cstddef>
#include <string>
#include <vector>

// Research collector support only. The engine's normal search is unchanged.
namespace regicide::research {
struct EvaluationWitness {
    std::string fen;
    std::vector<Move> path;
    StaticEvaluationObservation observation;
    Color turn;
    std::size_t history_size;
    std::uint64_t repetition_context;
    int repetition_count;
    bool in_check;
    Outcome outcome;
};

inline EvaluationWitness witness(const Board& board, const std::vector<Move>& path,
                                 const StaticEvaluationObservation& observation) {
    return {board.fen(), path, observation, board.position().turn, board.history_size(),
            board.repetition_context_key(), board.repetition_count(), board.in_check(), board.outcome()};
}

struct SupportDecision {
    bool accepted = false;
    std::string reason;
    std::size_t witness_index = 0;
    int root_white_score = 0;
};

inline bool current_claim(const Board& board) {
    return board.position().halfmove >= 100 || board.repetition_count() >= 3;
}

// An exact PV endpoint is necessary but not sufficient. It must coincide with
// an actual static invocation in the completed final iteration, and its raw
// integer value must be the value backed up all the way to the root. In a
// quiescence search this can be stand pat, not an assertion that every listed
// move would be played in a real game. Draw/mate/clamp/TT/scout values are not
// raw NNUE training labels and are rejected conservatively.
inline SupportDecision support_leaf(const Board& root, Move forced_root_move,
                                    int requested_depth, const SearchOptions& options,
                                    const SearchResult& result,
                                    const std::vector<EvaluationWitness>& observations,
                                    const NeuralModel& model) {
    const auto reject = [](std::string reason) { return SupportDecision{false, std::move(reason), 0, 0}; };
    if (!options.nnue || options.transposition || options.pvs || options.lmr ||
        !options.root_moves || *options.root_moves != std::vector<Move>{forced_root_move})
        return reject("unsupported_search_options");
    if (root.outcome() != Outcome::ongoing) return reject("terminal_root");
    if (current_claim(root)) return reject("root_draw_claim");
    if (result.stopped || result.depth != requested_depth) return reject("incomplete_requested_depth");
    if (result.tt_cutoffs != 0 || result.tt_probes != 0 || result.pvs_probes != 0 || result.lmr_reductions != 0)
        return reject("non_exact_search_path");
    if (!result.best_move || result.best_move != forced_root_move || result.pv.empty() ||
        result.pv.front() != forced_root_move)
        return reject("forced_root_or_pv_mismatch");
    if (result.score <= -mate_score + max_search_ply || result.score >= mate_score - max_search_ply)
        return reject("mate_band_root_score");
    Board leaf = root;
    for (const auto move : result.pv) {
        leaf.play(move);
        if (leaf.outcome() != Outcome::ongoing) return reject("terminal_pv_position");
        if (current_claim(leaf)) return reject("pv_draw_claim");
    }
    if (leaf.in_check()) return reject("checked_pv_endpoint");
    for (std::size_t i = observations.size(); i > 0; --i) {
        const auto& item = observations[i - 1];
        const auto& observation = item.observation;
        if (observation.iteration_depth != requested_depth || item.path != result.pv) continue;
        if (observation.ply != static_cast<int>(result.pv.size()) || item.fen != leaf.fen() ||
            item.turn != leaf.position().turn || item.history_size != leaf.history_size() ||
            item.repetition_context != leaf.repetition_context_key() ||
            item.repetition_count != leaf.repetition_count())
            return reject("witness_replay_mismatch");
        if (item.in_check || item.outcome != Outcome::ongoing || observation.current_draw_claim ||
            observation.draw_claim_adjusted)
            return reject("ineligible_static_witness");
        if (observation.raw_score != observation.score) return reject("clamped_static_witness");
        if (evaluate_neural(leaf, model) != observation.raw_score)
            return reject("full_refresh_witness_mismatch");
        const int leaf_white_score = leaf.position().turn == Color::white ?
                                    observation.raw_score : -observation.raw_score;
        const int root_white_score = root.position().turn == Color::white ? result.score : -result.score;
        if (leaf_white_score != root_white_score) return reject("backed_score_mismatch");
        return {true, "exact_static_support", i - 1, root_white_score};
    }
    return reject("no_final_iteration_pv_static_witness");
}
} // namespace regicide::research
