#pragma once

#include "regicide/board.hpp"

#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>

namespace regicide {

inline constexpr int max_hash_mb = 256;

// A position alone is insufficient: a previous repetition or the 50-move
// counter can change the value of an otherwise identical position.
struct TranspositionKey {
    std::uint64_t position = 0;
    std::uint64_t repetition_context = 0;
    int halfmove = 0;
    bool operator==(const TranspositionKey&) const = default;
};

enum class TranspositionBound { exact, lower, upper };

// Mate scores in the table measure distance from this node, not the root.
int score_to_table(int score, int ply);
int score_from_table(int score, int ply);

struct TranspositionEntry {
    TranspositionKey key;
    int depth = -1; // -1 marks an unused slot.
    int score = 0; // Normalized with score_to_table().
    TranspositionBound bound = TranspositionBound::exact;
    std::optional<Move> best_move;

    // Equal remaining depth preserves the fixed-depth horizon semantics.
    // A deeper result may still supply move ordering, but is not the same
    // mathematical query as a shallower result.
    std::optional<int> cutoff_score(int requested_depth, int ply, int alpha, int beta) const;
};

// Direct-mapped, depth-preferred replacement. All key fields are checked after
// indexing; an index collision can only evict work, never return another key.
// A search owns its table and reuses it only across its iterative depths.
class TranspositionTable {
public:
    explicit TranspositionTable(int megabytes);
    const TranspositionEntry* probe(const TranspositionKey& key) const;
    bool store(const TranspositionKey& key, int depth, int score, int ply,
               TranspositionBound bound, std::optional<Move> best_move);
    std::size_t capacity() const { return entries_.size(); }
    std::size_t allocated_bytes() const { return entries_.size() * sizeof(TranspositionEntry); }

private:
    std::vector<TranspositionEntry> entries_;
    std::size_t index(const TranspositionKey& key) const;
};

} // namespace regicide
