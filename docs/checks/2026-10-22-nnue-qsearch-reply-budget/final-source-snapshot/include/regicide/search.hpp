#pragma once

#include "regicide/board.hpp"
#include "regicide/transposition.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <vector>

namespace regicide {

class NeuralEvaluator;
struct NeuralModel;

inline constexpr int mate_score = 30000;
inline constexpr int max_search_depth = 64;
inline constexpr int max_quiescence_depth = 32;
inline constexpr int max_search_ply = max_search_depth + max_quiescence_depth;

// Positive scores favor the side to move; one pawn is 100 centipawns.
int evaluate_material(const Board& board);

struct SearchLimits {
    int depth = 4;
    std::uint64_t nodes = 0; // Zero means unlimited.
    std::chrono::milliseconds movetime{0}; // Zero means unlimited.
};

// An actual static-evaluator invocation, not a principal-variation endpoint.
// References passed to the observer are valid only for that invocation. The
// path starts at the search root; the caller retains the root's prior history.
// Unfinished iterative-deepening work is observed too. Root fallback, terminal
// adjudication, checked quiescence nodes and transposition returns are not.
struct StaticEvaluationObservation {
    int raw_score = 0; // Side-to-move evaluator output, before mate-band clamp.
    int score = 0; // The static score used by search, before any draw adjustment.
    int iteration_depth = 0;
    int ply = 0;
    int qply = -1; // -1 = normal horizon; >=0 = quiescence stand pat.
    std::uint64_t evaluation_index = 0; // One-based, includes excluded claims.
    bool current_draw_claim = false;
    bool draw_claim_adjusted = false; // Search substitutes an available zero.
};
using StaticEvaluationObserver = std::function<void(
    const Board&, const std::vector<Move>&, const StaticEvaluationObservation&)>;

struct SearchOptions {
    bool positional = true;
    bool quiescence = true;
    bool transposition = true;
    int hash_mb = 16; // Fresh scores per search; accepted range is 1..256 MiB.
    bool fast_moves = true; // Only for moves just returned by legal_moves().
    bool quiet_ordering = true; // Search-local killer/history ordering, no pruning.
    bool quiescence_checks = true; // One optional quiet check; all evasions remain.
    bool pvs = true; // Full first move, scout later moves; re-search improvements.
    bool see_ordering = true; // Local exchange estimates order moves only; no pruning.
    bool king_pawn_shield = true; // Middlegame shelter, scaled by opposing pieces.
    bool pawn_structure = true; // Passed pawns, doubled files and isolated pawns.
    bool lmr = true; // Late N/B/R/Q quiet reduction; requires PVS and quiet ordering.
    // Optional immutable experimental model. Null preserves the classical path;
    // positional=false still explicitly selects material-only evaluation.
    std::shared_ptr<const NeuralEvaluator> neural = {};
    // Independent integer NNUE experiment. Explicit NNUE overrides positional;
    // combining it with a residual/replacement neural model is an error.
    bool nnue = false;
    bool nnue_incremental = true; // False selects the exact full-refresh oracle.
    std::shared_ptr<const NeuralModel> nnue_model{}; // Null selects the untrained builtin.
    // Null searches every legal root move. A present list must be nonempty,
    // legal and duplicate-free. Descendants always retain all legal moves.
    std::optional<std::vector<Move>> root_moves = {};
    // Research-only opt-in. Empty leaves search decisions and traversal intact.
    // The observer must not mutate search-owned objects or stop state. Its work
    // consumes wall time, so deterministic collection uses node/depth limits.
    StaticEvaluationObserver static_evaluation_observer = {};
    // Experimental bounded knight-fork threats plus guarded local SEE pruning
    // in quiescence. One quiet threat per path; all opponent replies remain.
    // SEE pruning excludes check evasions, threat replies, checks, EP and promotions.
    bool quiescence_threats = false;
    // Research-only, independent of the normal-node TT. A search-local 1 MiB
    // cache reuses completed qsearch bounds with the same draw and extension
    // context. Disabled by default; it removes no tactical candidates.
    bool quiescence_cache = false;
    // Research-only first-iteration root ordering. Quiet knight forks and safe
    // capture-fork preparation precede the existing order; no move is removed.
    bool tactical_threat_ordering = false;
    // Research-only horizon policy. Initiate optional quiet checks and knight
    // threats only within qply 0..2. Captures/promotions, all check evasions and
    // every immediate reply to an already launched threat keep their policy.
    // This is selective: a quiet threat first appearing later can be missed.
    bool quiescence_horizon_budget = false;
};

struct SearchResult {
    std::optional<Move> best_move;
    int score = 0;
    int depth = 0; // Last completely searched iteration; zero is a legal fallback.
    std::uint64_t nodes = 0; // Includes unfinished work and iterative roots.
    std::uint64_t qnodes = 0; // Quiescence nodes are a subset of nodes.
    std::uint64_t tt_probes = 0;
    std::uint64_t tt_hits = 0; // All key fields match, even if depth is unusable.
    std::uint64_t tt_cutoffs = 0;
    std::uint64_t tt_stores = 0; // Completed normal nodes accepted by replacement.
    std::uint64_t tt_allocations = 0; // New backing buffer allocations this call.
    std::uint64_t tt_reuses = 0; // Existing same-size buffer reused this call.
    std::uint64_t tt_clears = 0; // Physical full clears on generation wrap.
    std::uint64_t tt_bytes = 0; // Active table storage, zero if disabled/terminal.
    std::uint64_t pvs_probes = 0; // Later-move null-window calls, including LMR retries/unfinished work.
    std::uint64_t pvs_researches = 0; // Probes needing a full-window re-search.
    std::uint64_t see_calls = 0; // Tactical candidates needing an exchange key; no sole/preferred move work.
    std::uint64_t lmr_reductions = 0; // Reduced scouts attempted, including unfinished work.
    std::uint64_t lmr_researches = 0; // Reduced improvements rechecked by a full-depth scout.
    int seldepth = 0; // Deepest visited ply, including unfinished work.
    std::chrono::milliseconds elapsed{0};
    bool stopped = false;
    std::vector<Move> pv;
    std::uint64_t quiet_threat_candidates = 0; // Qualified quiet moves, including pruned siblings.
    std::uint64_t quiet_threat_extensions = 0; // Quiet threat branches actually searched.
    std::uint64_t defensive_qnodes = 0; // All-reply quiescence nodes after such a threat.
    std::uint64_t qsee_pruned = 0; // Locally losing ordinary nonchecking q captures omitted by the opt-in.
    std::uint64_t qcache_probes = 0;
    std::uint64_t qcache_hits = 0; // Draw state and all q extension budgets match.
    std::uint64_t qcache_cutoffs = 0;
    std::uint64_t qcache_stores = 0; // Completed nodes only, including stand pat.
    std::uint64_t qcache_bytes = 0; // At most 1 MiB, separate from normal Hash.
    std::uint64_t root_threat_candidates = 0;
    std::uint64_t root_threat_searches = 0;
};

using SearchCallback = std::function<void(const SearchResult&)>;

// Caller-owned storage for one worker, never shared by concurrent searches.
// Only memory is reused: each search logically discards every old score and
// move, so changing positions, history, options or limits cannot leak results.
class SearchContext {
public:
    SearchContext() = default;
    SearchContext(const SearchContext&) = delete;
    SearchContext& operator=(const SearchContext&) = delete;
    SearchContext(SearchContext&&) = delete;
    SearchContext& operator=(SearchContext&&) = delete;
    void clear() { (void)table_.clear(); }

private:
    friend SearchResult search(const Board&, const SearchLimits&, const std::atomic_bool*,
                               SearchCallback, SearchOptions, SearchContext*);
    TranspositionTable table_;
};

// Searches a copy, including repetition history. Terminal roots have no move.
// Optional current/intended draw claims are available at value zero; callers
// must actually claim the draw when choosing that option (UCI cannot claim).
// Invalid limits or simultaneous neural/nnue selection throw invalid_argument.
SearchResult search(const Board& board, const SearchLimits& limits,
                    const std::atomic_bool* stop = nullptr,
                    SearchCallback on_iteration = {}, SearchOptions options = {},
                    SearchContext* context = nullptr);

} // namespace regicide
