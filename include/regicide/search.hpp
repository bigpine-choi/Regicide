#pragma once

#include "regicide/board.hpp"
#include "regicide/transposition.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <optional>
#include <vector>

namespace regicide {

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

struct SearchOptions {
    bool positional = true;
    bool quiescence = true;
    bool transposition = true;
    int hash_mb = 16; // Fresh scores per search; accepted range is 1..256 MiB.
    bool fast_moves = true; // Only for moves just returned by legal_moves().
    bool quiet_ordering = true; // Search-local killer/history ordering, no pruning.
    bool quiescence_checks = true; // One optional quiet check; all evasions remain.
    bool pvs = true; // Full first move, scout later moves; re-search improvements.
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
    std::uint64_t pvs_probes = 0; // Later-move null-window searches, including unfinished work.
    std::uint64_t pvs_researches = 0; // Probes needing a full-window re-search.
    int seldepth = 0; // Deepest visited ply, including unfinished work.
    std::chrono::milliseconds elapsed{0};
    bool stopped = false;
    std::vector<Move> pv;
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
// Invalid depth (outside 1..64) or negative movetime throws invalid_argument.
SearchResult search(const Board& board, const SearchLimits& limits,
                    const std::atomic_bool* stop = nullptr,
                    SearchCallback on_iteration = {}, SearchOptions options = {},
                    SearchContext* context = nullptr);

} // namespace regicide
