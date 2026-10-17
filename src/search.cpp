#include "regicide/search.hpp"
#include "regicide/evaluation.hpp"
#include "regicide/transposition.hpp"

#include <algorithm>
#include <stdexcept>
#include <tuple>
#include <utility>

namespace regicide {
namespace {
using Clock = std::chrono::steady_clock;
constexpr int infinity = 32000;

int value(char piece) {
    switch (piece) {
    case 'P': case 'p': return 100;
    case 'N': case 'n': return 320;
    case 'B': case 'b': return 330;
    case 'R': case 'r': return 500;
    case 'Q': case 'q': return 900;
    default: return 0;
    }
}

bool claimable(const Board& board) {
    return board.position().halfmove >= 100 || board.repetition_count() >= 3;
}

bool automatic_draw(const Board& board) {
    return board.has_known_dead_material() || board.position().halfmove >= 150 ||
           board.repetition_count() >= 5;
}

void order_moves(const Board& board, std::vector<Move>& moves,
                 const std::optional<Move>& preferred = {}) {
    const auto& position = board.position();
    const auto priority = [&](Move move) {
        int captured = value(position.squares[move.to]);
        const char piece = position.squares[move.from];
        if ((piece == 'P' || piece == 'p') && move.to == position.en_passant)
            captured = 100;
        int result = captured ? 16 * captured - value(piece) : 0;
        if (move.promotion) result += 16 * value(move.promotion);
        if (preferred && move == *preferred) result += 1000000;
        return result;
    };
    std::sort(moves.begin(), moves.end(), [&](Move left, Move right) {
        const int left_priority = priority(left), right_priority = priority(right);
        if (left_priority != right_priority) return left_priority > right_priority;
        return std::tie(left.from, left.to, left.promotion) <
               std::tie(right.from, right.to, right.promotion);
    });
}

// Restores board and repetition history, including when cancellation unwinds.
class AppliedMove {
public:
    AppliedMove(Board& board, Move move, bool fast) : board_(board) {
        if (fast) board_.play_generated(move);
        else board_.play(move);
    }
    ~AppliedMove() { board_.undo(); }
    AppliedMove(const AppliedMove&) = delete;
    AppliedMove& operator=(const AppliedMove&) = delete;
private:
    Board& board_;
};

struct Interrupted {};

class Searcher {
public:
    Searcher(const SearchLimits& limits, const std::atomic_bool* stop, Clock::time_point start,
             SearchOptions options)
        : limits_(limits), stop_(stop), start_(start), options_(options) {
        if (options.transposition) table_.emplace(options.hash_mb);
    }

    std::uint64_t nodes = 0;
    std::uint64_t qnodes = 0;
    std::uint64_t tt_probes = 0, tt_hits = 0, tt_cutoffs = 0, tt_stores = 0;
    int seldepth = 0;

    int negamax(Board& board, int depth, int ply, int alpha, int beta,
                std::vector<Move>& pv, const std::optional<Move>& preferred = {}) {
        // A horizon position belongs to exactly one node, even when extended.
        if (depth == 0 && options_.quiescence)
            return quiescence(board, ply, 0, alpha, beta, pv);
        poll();
        ++nodes;
        seldepth = std::max(seldepth, ply);
        pv.clear();
        auto moves = board.legal_moves();
        // A mate on the last searched ply outranks material and draw counters.
        if (moves.empty()) return board.in_check() ? -mate_score + ply : 0;
        if (automatic_draw(board)) return 0;
        const bool current_claim = claimable(board);
        if (depth == 0) {
            const int score = static_evaluation(board);
            if (score < 0 && (current_claim || intended_claim_available(board, moves))) return 0;
            return score;
        }
        const int original_alpha = alpha;
        const TranspositionKey key{board.position_key(), board.repetition_context_key(),
                                   board.position().halfmove};
        std::optional<Move> ordered_preferred = preferred;
        if (table_) {
            ++tt_probes;
            if (const auto* entry = table_->probe(key)) {
                ++tt_hits;
                // Verify a cached move before putting it in a published PV.
                // Never return a cached root result: the completed iteration
                // must always provide an explicitly searched legal root move.
                if (entry->best_move &&
                    std::find(moves.begin(), moves.end(), *entry->best_move) != moves.end()) {
                    ordered_preferred = entry->best_move;
                    if (ply > 0) {
                        if (const auto score = entry->cutoff_score(depth, ply, alpha, beta)) {
                            ++tt_cutoffs;
                            pv.push_back(*entry->best_move);
                            return *score;
                        }
                    }
                }
            }
        }
        if (current_claim) {
            alpha = std::max(alpha, 0);
            if (alpha >= beta) return 0;
        }
        order_moves(board, moves, ordered_preferred);
        int best = -infinity;
        for (Move move : moves) {
            poll(false);
            int score;
            std::vector<Move> child_pv;
            {
                AppliedMove applied(board, move, options_.fast_moves);
                const bool intended_claim = claimable(board);
                score = -negamax(board, depth - 1, ply + 1, -beta, -alpha, child_pv);
                // The mover may claim before an intended move that creates a
                // third repetition or completes 50 moves, even if the opponent
                // would decline a claim and continue with a winning position.
                if (intended_claim) score = std::max(0, score);
            }
            if (score > best) {
                best = score;
                pv.clear();
                pv.push_back(move);
                pv.insert(pv.end(), child_pv.begin(), child_pv.end());
            }
            alpha = std::max(alpha, score);
            if (alpha >= beta) break;
        }
        const int score = current_claim ? std::max(0, best) : best;
        if (table_) {
            const auto bound = score <= original_alpha ? TranspositionBound::upper :
                               score >= beta ? TranspositionBound::lower : TranspositionBound::exact;
            // Interrupted recursion unwinds before this point, so partial
            // nodes can never masquerade as completed scores or bounds.
            if (table_->store(key, depth, score, ply, bound, pv.front())) ++tt_stores;
        }
        return score;
    }

private:
    const SearchLimits& limits_;
    const std::atomic_bool* stop_;
    Clock::time_point start_;
    SearchOptions options_;
    std::optional<TranspositionTable> table_;

    int static_evaluation(const Board& board) const {
        const int score = options_.positional ? evaluate_position(board) : evaluate_material(board);
        const int reserved_plies = options_.quiescence ? max_search_ply : max_search_depth;
        return std::clamp(score, -mate_score + reserved_plies + 1,
                          mate_score - reserved_plies - 1);
    }

    int quiescence(Board& board, int ply, int qply, int alpha, int beta,
                   std::vector<Move>& pv) {
        poll();
        ++nodes;
        ++qnodes;
        seldepth = std::max(seldepth, ply);
        pv.clear();
        auto moves = board.legal_moves();
        const bool checked = board.in_check();
        if (moves.empty()) return checked ? -mate_score + ply : 0;
        if (automatic_draw(board)) return 0;
        const bool current_claim = claimable(board);
        // No position in check may use stand pat. If the absolute extension
        // ceiling is reached, discard this iteration and retain the previous
        // complete result, rather than treating an unresolved check as quiet.
        if (checked && qply >= max_quiescence_depth) throw Interrupted{};

        int best = -infinity;
        if (!checked) {
            best = static_evaluation(board);
            // A quiet intended move can permit a draw even though quiescence
            // otherwise searches only captures and promotions.
            if (best < 0 && (current_claim || intended_claim_available(board, moves))) best = 0;
            if (best >= beta || qply >= max_quiescence_depth) return best;
            alpha = std::max(alpha, best);
            const auto& position = board.position();
            std::erase_if(moves, [&](Move move) {
                const char piece = position.squares[move.from];
                const bool en_passant = (piece == 'P' || piece == 'p') &&
                                        move.to == position.en_passant;
                return position.squares[move.to] == '.' && !en_passant && !move.promotion;
            });
        } else if (current_claim) {
            best = 0;
            alpha = std::max(alpha, 0);
            if (alpha >= beta) return 0;
        }
        order_moves(board, moves);
        for (Move move : moves) {
            poll(false);
            int score;
            std::vector<Move> child_pv;
            {
                AppliedMove applied(board, move, options_.fast_moves);
                const bool intended_claim = claimable(board);
                score = -quiescence(board, ply + 1, qply + 1, -beta, -alpha, child_pv);
                if (intended_claim) score = std::max(0, score);
            }
            if (score > best) {
                best = score;
                pv.clear();
                pv.push_back(move);
                pv.insert(pv.end(), child_pv.begin(), child_pv.end());
            }
            alpha = std::max(alpha, score);
            if (alpha >= beta) break;
        }
        return best;
    }

    bool intended_claim_available(Board& board, const std::vector<Move>& moves) const {
        // A third occurrence is impossible unless some reversible-history
        // position has already occurred twice. The board maintains this fact
        // exactly, avoiding a full speculative move scan at most q leaves.
        if (board.position().halfmove < 99 && !board.has_repetition_candidate()) return false;
        for (Move move : moves) {
            poll(false);
            AppliedMove applied(board, move, options_.fast_moves);
            if (claimable(board) && board.outcome() == Outcome::ongoing) return true;
        }
        return false;
    }

    void poll(bool entering_node = true) const {
        if ((stop_ && stop_->load(std::memory_order_relaxed)) ||
            (entering_node && limits_.nodes && nodes >= limits_.nodes) ||
            (limits_.movetime.count() && Clock::now() - start_ >= limits_.movetime))
            throw Interrupted{};
    }
};
} // namespace

int evaluate_material(const Board& board) {
    int score = 0;
    for (char piece : board.position().squares)
        score += piece >= 'A' && piece <= 'Z' ? value(piece) : -value(piece);
    return board.position().turn == Color::white ? score : -score;
}

SearchResult search(const Board& input, const SearchLimits& limits,
                    const std::atomic_bool* stop, SearchCallback on_iteration,
                    SearchOptions options) {
    if (limits.depth < 1 || limits.depth > max_search_depth)
        throw std::invalid_argument("search depth must be 1..64");
    if (limits.movetime.count() < 0)
        throw std::invalid_argument("search movetime cannot be negative");
    if (options.hash_mb < 1 || options.hash_mb > max_hash_mb)
        throw std::invalid_argument("hash size must be 1..256 MiB");
    const auto start = Clock::now();
    SearchResult result;
    Board board = input;
    auto moves = board.legal_moves();
    const auto elapsed = [&] {
        return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start);
    };
    if (moves.empty() || automatic_draw(board)) {
        result.score = moves.empty() && board.in_check() ? -mate_score : 0;
        result.elapsed = elapsed();
        return result;
    }
    order_moves(board, moves);
    result.best_move = moves.front();
    result.pv.push_back(moves.front());
    result.score = options.positional ? evaluate_position(board) : evaluate_material(board);
    if (claimable(board)) result.score = std::max(0, result.score);
    Searcher searcher(limits, stop, start, options);
    for (int depth = 1; depth <= limits.depth; ++depth) {
        std::vector<Move> pv;
        int score;
        try {
            score = searcher.negamax(board, depth, 0, -infinity, infinity, pv, result.best_move);
        } catch (const Interrupted&) {
            result.stopped = true;
            break;
        }
        result.best_move = pv.front();
        result.pv = std::move(pv);
        result.score = score;
        result.depth = depth;
        result.nodes = searcher.nodes;
        result.qnodes = searcher.qnodes;
        result.tt_probes = searcher.tt_probes;
        result.tt_hits = searcher.tt_hits;
        result.tt_cutoffs = searcher.tt_cutoffs;
        result.tt_stores = searcher.tt_stores;
        result.seldepth = searcher.seldepth;
        result.elapsed = elapsed();
        if (on_iteration) on_iteration(result);
    }
    result.nodes = searcher.nodes;
    result.qnodes = searcher.qnodes;
    result.tt_probes = searcher.tt_probes;
    result.tt_hits = searcher.tt_hits;
    result.tt_cutoffs = searcher.tt_cutoffs;
    result.tt_stores = searcher.tt_stores;
    result.seldepth = searcher.seldepth;
    result.elapsed = elapsed();
    return result;
}

} // namespace regicide
