#include "regicide/search.hpp"
#include "regicide/evaluation.hpp"
#include "regicide/see.hpp"
#include "regicide/transposition.hpp"

#include <algorithm>
#include <array>
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

bool quiet_move(const Board& board, Move move) {
    const auto& position = board.position();
    const char piece = position.squares[move.from];
    const bool en_passant = (piece == 'P' || piece == 'p') && move.to == position.en_passant;
    return position.squares[move.to] == '.' && !en_passant && !move.promotion;
}

using QuietHistory = std::array<std::array<int, 64>, 64>;
using Killers = std::array<std::optional<Move>, 2>;
constexpr int history_limit = 16384;

void order_moves(const Board& board, std::vector<Move>& moves,
                 const std::optional<Move>& preferred = {},
                 const Killers* killers = nullptr, const QuietHistory* history = nullptr) {
    const auto& position = board.position();
    const auto priority = [&](Move move) {
        int captured = value(position.squares[move.to]);
        const char piece = position.squares[move.from];
        if ((piece == 'P' || piece == 'p') && move.to == position.en_passant)
            captured = 100;
        int result = captured ? 16 * captured - value(piece) : 0;
        if (move.promotion) result += 16 * value(move.promotion);
        if (killers && history) {
            if (!quiet_move(board, move)) result += 1000000;
            else {
                if ((*killers)[0] == move) result += 200000;
                else if ((*killers)[1] == move) result += 100000;
                result += (*history)[move.from][move.to];
            }
            if (preferred && move == *preferred) result += 10000000;
        } else if (preferred && move == *preferred) result += 1000000;
        return result;
    };
    std::sort(moves.begin(), moves.end(), [&](Move left, Move right) {
        const int left_priority = priority(left), right_priority = priority(right);
        if (left_priority != right_priority) return left_priority > right_priority;
        return std::tie(left.from, left.to, left.promotion) <
               std::tie(right.from, right.to, right.promotion);
    });
}

template<class Poll>
void order_moves_with_see(const Board& board, std::vector<Move>& moves,
                          const std::optional<Move>& preferred,
                          const Killers* killers, const QuietHistory* history,
                          std::uint64_t& see_calls, Poll&& poll) {
    // There is no ordering decision for zero or one legal candidate. Keep
    // cancellation responsive without evaluating an exchange we cannot use.
    if (moves.size() <= 1) {
        poll();
        return;
    }
    struct OrderedMove {
        Move move;
        int group; // Preferred, non-losing tactical, quiet, losing tactical.
        int exchange;
        int priority;
    };
    const auto& position = board.position();
    std::vector<OrderedMove> ordered;
    ordered.reserve(moves.size());
    for (Move move : moves) {
        poll();
        const bool quiet = quiet_move(board, move);
        const bool is_preferred = preferred && move == *preferred;
        int exchange = 0, priority = 0;
        // A preferred move has a unique, higher group. Its exchange/secondary
        // score can never enter a comparison, so it needs no SEE calculation.
        if (!quiet && !is_preferred) {
            // Decorate once: sorting comparisons never repeat exchange work.
            exchange = static_exchange_evaluation(board, move);
            ++see_calls;
            int captured = value(position.squares[move.to]);
            const char piece = position.squares[move.from];
            if ((piece == 'P' || piece == 'p') && move.to == position.en_passant) captured = 100;
            priority = captured ? 16 * captured - value(piece) : 0;
            if (move.promotion) priority += 16 * value(move.promotion);
        } else if (quiet && killers && history) {
            if ((*killers)[0] == move) priority += 200000;
            else if ((*killers)[1] == move) priority += 100000;
            priority += (*history)[move.from][move.to];
        }
        const int group = is_preferred ? 3 :
                          quiet ? 1 : exchange >= 0 ? 2 : 0;
        ordered.push_back({move, group, exchange, priority});
    }
    std::sort(ordered.begin(), ordered.end(), [](const OrderedMove& left, const OrderedMove& right) {
        if (left.group != right.group) return left.group > right.group;
        if (left.exchange != right.exchange) return left.exchange > right.exchange;
        if (left.priority != right.priority) return left.priority > right.priority;
        return std::tie(left.move.from, left.move.to, left.move.promotion) <
               std::tie(right.move.from, right.move.to, right.move.promotion);
    });
    for (std::size_t i = 0; i < moves.size(); ++i) moves[i] = ordered[i].move;
    poll();
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
             SearchOptions options, TranspositionTable* table)
        : limits_(limits), stop_(stop), start_(start), options_(options), table_(table) {}

    std::uint64_t nodes = 0;
    std::uint64_t qnodes = 0;
    std::uint64_t tt_probes = 0, tt_hits = 0, tt_cutoffs = 0, tt_stores = 0;
    std::uint64_t pvs_probes = 0, pvs_researches = 0;
    std::uint64_t see_calls = 0;
    int seldepth = 0;

    int negamax(Board& board, int depth, int ply, int alpha, int beta,
                std::vector<Move>& pv, const std::optional<Move>& preferred = {}) {
        // A horizon position belongs to exactly one node, even when extended.
        if (depth == 0 && options_.quiescence)
            return quiescence(board, ply, 0, options_.quiescence_checks ? 1 : 0, alpha, beta, pv);
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
        const auto color = board.position().turn == Color::white ? 0U : 1U;
        if (options_.see_ordering)
            order_moves_with_see(board, moves, ordered_preferred,
                                 options_.quiet_ordering ? &killers_[ply] : nullptr,
                                 options_.quiet_ordering ? &history_[color] : nullptr,
                                 see_calls, [this] { poll(false); });
        else if (options_.quiet_ordering)
            order_moves(board, moves, ordered_preferred, &killers_[ply], &history_[color]);
        else order_moves(board, moves, ordered_preferred);
        int best = -infinity;
        bool first_move = true;
        for (Move move : moves) {
            poll(false);
            int score;
            std::vector<Move> child_pv;
            {
                AppliedMove applied(board, move, options_.fast_moves);
                const bool intended_claim = claimable(board);
                if (options_.pvs && !first_move) {
                    // A one-point window first asks only whether this move can
                    // improve alpha. An improvement below beta needs a normal
                    // search before publishing its score or principal variation.
                    ++pvs_probes;
                    score = -negamax(board, depth - 1, ply + 1, -alpha - 1, -alpha, child_pv);
                    // The mover's intended claim belongs to this move's value,
                    // so account for it before deciding whether to re-search.
                    if (intended_claim) score = std::max(0, score);
                    if (score > alpha && score < beta) {
                        ++pvs_researches;
                        score = -negamax(board, depth - 1, ply + 1, -beta, -alpha, child_pv);
                    }
                } else {
                    score = -negamax(board, depth - 1, ply + 1, -beta, -alpha, child_pv);
                }
                // The mover may claim before an intended move that creates a
                // third repetition or completes 50 moves, even if the opponent
                // would decline a claim and continue with a winning position.
                if (intended_claim) score = std::max(0, score);
            }
            first_move = false;
            if (score > best) {
                best = score;
                pv.clear();
                pv.push_back(move);
                pv.insert(pv.end(), child_pv.begin(), child_pv.end());
            }
            alpha = std::max(alpha, score);
            if (alpha >= beta) {
                if (options_.quiet_ordering && quiet_move(board, move)) {
                    auto& killers = killers_[ply];
                    if (killers[0] != move) { killers[1] = killers[0]; killers[0] = move; }
                    auto& history = history_[color][move.from][move.to];
                    const int reward = depth * depth;
                    history += reward - history * reward / history_limit;
                }
                break;
            }
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
    TranspositionTable* table_;
    std::array<Killers, max_search_ply + 1> killers_{};
    std::array<QuietHistory, 2> history_{}; // Bounded, reset for every search.

    int static_evaluation(const Board& board) const {
        const int score = options_.neural ? evaluate_neural(board) :
            (options_.positional ? evaluate_position(board) : evaluate_material(board));
        const int reserved_plies = options_.quiescence ? max_search_ply : max_search_depth;
        return std::clamp(score, -mate_score + reserved_plies + 1,
                          mate_score - reserved_plies - 1);
    }

    int quiescence(Board& board, int ply, int qply, int checks_left, int alpha, int beta,
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
            std::erase_if(moves, [&](Move move) {
                return quiet_move(board, move) &&
                       !(checks_left > 0 && board.gives_check_generated(move));
            });
        } else if (current_claim) {
            best = 0;
            alpha = std::max(alpha, 0);
            if (alpha >= beta) return 0;
        }
        if (options_.see_ordering)
            order_moves_with_see(board, moves, {}, nullptr, nullptr, see_calls, [this] { poll(false); });
        else order_moves(board, moves);
        for (Move move : moves) {
            poll(false);
            int score;
            const bool quiet = quiet_move(board, move);
            std::vector<Move> child_pv;
            {
                AppliedMove applied(board, move, options_.fast_moves);
                const bool intended_claim = claimable(board);
                // Captures/promotions preserve the quiet-check budget. All
                // check evasions remain legal candidates even at zero; a
                // quiet countercheck consumes the budget only if one remains.
                const int child_checks = checks_left -
                    (checks_left > 0 && quiet && board.in_check() ? 1 : 0);
                score = -quiescence(board, ply + 1, qply + 1, child_checks, -beta, -alpha, child_pv);
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
                    SearchOptions options, SearchContext* context) {
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
    result.score = options.neural ? evaluate_neural(board) :
        (options.positional ? evaluate_position(board) : evaluate_material(board));
    if (claimable(board)) result.score = std::max(0, result.score);
    SearchContext one_shot_context;
    auto& active_context = context ? *context : one_shot_context;
    TranspositionTable* table = nullptr;
    if (options.transposition) {
        const auto work = active_context.table_.prepare(options.hash_mb);
        result.tt_allocations = work.allocations;
        result.tt_reuses = work.reuses;
        result.tt_clears = work.clears;
        result.tt_bytes = work.bytes;
        table = &active_context.table_;
    }
    Searcher searcher(limits, stop, start, options, table);
    for (int depth = 1; depth <= limits.depth; ++depth) {
        std::vector<Move> pv;
        int score;
        try {
            // Before the first iteration, best_move is only a cheap legal
            // cancellation fallback, not a searched PV. Let SEE choose that
            // iteration's first candidate; later completed PVs remain first.
            // The disabled path preserves the previous version's exact order.
            const auto preferred = depth == 1 && options.see_ordering ?
                                   std::optional<Move>{} : result.best_move;
            score = searcher.negamax(board, depth, 0, -infinity, infinity, pv, preferred);
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
        result.pvs_probes = searcher.pvs_probes;
        result.pvs_researches = searcher.pvs_researches;
        result.see_calls = searcher.see_calls;
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
    result.pvs_probes = searcher.pvs_probes;
    result.pvs_researches = searcher.pvs_researches;
    result.see_calls = searcher.see_calls;
    result.seldepth = searcher.seldepth;
    result.elapsed = elapsed();
    return result;
}

} // namespace regicide
