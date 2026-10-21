#pragma once

#include "choice_leaf_support.hpp"

// Observed support under the engine's default search. This verifies an actual
// static invocation in the completed callback prefix with matching PV/history
// and root score. It does not claim a complete causal alpha-beta/TT trace.
namespace regicide::research {
inline SupportDecision live_support_leaf(const Board& root, int requested_depth,
                                        const SearchOptions& options,
                                        const SearchResult& final_result,
                                        const SearchResult& completed,
                                        std::size_t completed_observations,
                                        const std::vector<EvaluationWitness>& observations,
                                        const NeuralModel& model) {
    const auto reject = [](std::string reason) { return SupportDecision{false, std::move(reason), 0, 0}; };
    if (!options.nnue || !options.nnue_incremental || !options.quiescence ||
        !options.transposition || !options.pvs || !options.lmr || !options.fast_moves ||
        !options.quiet_ordering || !options.quiescence_checks || !options.see_ordering ||
        !options.positional || !options.king_pawn_shield || !options.pawn_structure ||
        options.hash_mb != 16 || options.neural ||
        (options.root_moves && options.root_moves->size() != 1))
        return reject("unsupported_default_search_options");
    if (root.outcome() != Outcome::ongoing) return reject("terminal_root");
    if (current_claim(root)) return reject("root_draw_claim");
    if (completed.depth <= 0 || completed.depth > requested_depth || completed.stopped ||
        completed.depth != final_result.depth || completed.score != final_result.score ||
        completed.best_move != final_result.best_move || completed.pv != final_result.pv ||
        completed.nodes > final_result.nodes || completed_observations > observations.size())
        return reject("missing_or_inconsistent_completed_iteration");
    if (!completed.best_move || completed.pv.empty() || completed.pv.front() != completed.best_move ||
        (options.root_moves && completed.best_move != options.root_moves->front()))
        return reject("root_or_pv_mismatch");
    if (completed.pv.size() < static_cast<std::size_t>(completed.depth))
        return reject("truncated_completed_pv");
    if (completed.score <= -mate_score + max_search_ply || completed.score >= mate_score - max_search_ply)
        return reject("mate_band_root_score");
    Board leaf = root;
    for (const auto move : completed.pv) {
        leaf.play(move);
        if (leaf.outcome() != Outcome::ongoing) return reject("terminal_pv_position");
        if (current_claim(leaf)) return reject("pv_draw_claim");
    }
    if (leaf.in_check()) return reject("checked_pv_endpoint");
    for (std::size_t i = completed_observations; i > 0; --i) {
        const auto& item = observations[i - 1];
        const auto& observation = item.observation;
        if (observation.iteration_depth != completed.depth || item.path != completed.pv) continue;
        if (observation.ply != static_cast<int>(completed.pv.size()) || item.fen != leaf.fen() ||
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
        const int leaf_white = item.turn == Color::white ? observation.raw_score : -observation.raw_score;
        const int root_white = root.position().turn == Color::white ? completed.score : -completed.score;
        if (leaf_white != root_white) return reject("backed_score_mismatch");
        return {true, "observed_completed_pv_support", i - 1, root_white};
    }
    return reject("no_completed_prefix_pv_static_witness");
}
} // namespace regicide::research
