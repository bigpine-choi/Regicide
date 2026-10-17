#include "regicide/transposition.hpp"
#include "regicide/search.hpp"

#include <bit>
#include <stdexcept>

namespace regicide {
namespace {
constexpr int mate_threshold = mate_score - max_search_ply;
}

int score_to_table(int score, int ply) {
    if (score >= mate_threshold) return score + ply;
    if (score <= -mate_threshold) return score - ply;
    return score;
}

int score_from_table(int score, int ply) {
    if (score >= mate_threshold) return score - ply;
    if (score <= -mate_threshold) return score + ply;
    return score;
}

std::optional<int> TranspositionEntry::cutoff_score(int requested_depth, int ply,
                                                   int alpha, int beta) const {
    if (depth != requested_depth || depth < 0) return {};
    const int decoded = score_from_table(score, ply);
    if (bound == TranspositionBound::exact ||
        (bound == TranspositionBound::lower && decoded >= beta) ||
        (bound == TranspositionBound::upper && decoded <= alpha)) return decoded;
    return {};
}

TranspositionTable::TranspositionTable(int megabytes) {
    if (megabytes < 1 || megabytes > max_hash_mb)
        throw std::invalid_argument("hash size must be 1..256 MiB");
    const auto bytes = static_cast<std::size_t>(megabytes) * 1024 * 1024;
    entries_.resize(bytes / sizeof(TranspositionEntry));
}

std::size_t TranspositionTable::index(const TranspositionKey& key) const {
    std::uint64_t mixed = key.position ^ std::rotl(key.repetition_context, 23) ^
        (static_cast<std::uint64_t>(key.halfmove) * UINT64_C(0x9e3779b97f4a7c15));
    mixed ^= mixed >> 30;
    mixed *= UINT64_C(0xbf58476d1ce4e5b9);
    mixed ^= mixed >> 27;
    mixed *= UINT64_C(0x94d049bb133111eb);
    mixed ^= mixed >> 31;
    return static_cast<std::size_t>(mixed % entries_.size());
}

const TranspositionEntry* TranspositionTable::probe(const TranspositionKey& key) const {
    const auto& entry = entries_[index(key)];
    return entry.depth >= 0 && entry.key == key ? &entry : nullptr;
}

bool TranspositionTable::store(const TranspositionKey& key, int depth, int score, int ply,
                               TranspositionBound bound, std::optional<Move> best_move) {
    auto& entry = entries_[index(key)];
    if (entry.depth > depth) return false;
    if (entry.depth == depth && entry.key == key &&
        entry.bound == TranspositionBound::exact && bound != TranspositionBound::exact)
        return false;
    entry = {key, depth, score_to_table(score, ply), bound, best_move};
    return true;
}

} // namespace regicide
