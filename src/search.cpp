#include "regicide/search.hpp"

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
    AppliedMove(Board& board, Move move) : board_(board) { board_.play(move); }
    ~AppliedMove() { board_.undo(); }
    AppliedMove(const AppliedMove&) = delete;
    AppliedMove& operator=(const AppliedMove&) = delete;
private:
    Board& board_;
};

struct Interrupted {};

class Searcher {
public:
    Searcher(const SearchLimits& limits, const std::atomic_bool* stop, Clock::time_point start)
        : limits_(limits), stop_(stop), start_(start) {}

    std::uint64_t nodes = 0;

    int negamax(Board& board, int depth, int ply, int alpha, int beta,
                std::vector<Move>& pv, const std::optional<Move>& preferred = {}) {
        poll();
        ++nodes;
        pv.clear();
        auto moves = board.legal_moves();
        // A mate on the last searched ply outranks material and draw counters.
        if (moves.empty()) return board.in_check() ? -mate_score + ply : 0;
        if (automatic_draw(board)) return 0;
        const bool current_claim = claimable(board);
        if (depth == 0) {
            const int score = std::clamp(evaluate_material(board),
                                         -mate_score + max_search_depth + 1,
                                         mate_score - max_search_depth - 1);
            if (score < 0 && (current_claim || intended_claim_available(board, moves))) return 0;
            return score;
        }
        if (current_claim) {
            alpha = std::max(alpha, 0);
            if (alpha >= beta) return 0;
        }
        order_moves(board, moves, preferred);
        int best = -infinity;
        for (Move move : moves) {
            int score;
            std::vector<Move> child_pv;
            {
                AppliedMove applied(board, move);
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
        return current_claim ? std::max(0, best) : best;
    }

private:
    const SearchLimits& limits_;
    const std::atomic_bool* stop_;
    Clock::time_point start_;

    bool intended_claim_available(Board& board, const std::vector<Move>& moves) const {
        // Three occurrences need at least two four-ply cycles. Avoid replaying
        // candidate moves when the known history is too short to claim.
        if (board.position().halfmove < 99 && board.history_size() < 7) return false;
        for (Move move : moves) {
            poll(false);
            AppliedMove applied(board, move);
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
                    const std::atomic_bool* stop, SearchCallback on_iteration) {
    if (limits.depth < 1 || limits.depth > max_search_depth)
        throw std::invalid_argument("search depth must be 1..64");
    if (limits.movetime.count() < 0)
        throw std::invalid_argument("search movetime cannot be negative");
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
    result.score = evaluate_material(board);
    if (claimable(board)) result.score = std::max(0, result.score);
    Searcher searcher(limits, stop, start);
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
        result.elapsed = elapsed();
        if (on_iteration) on_iteration(result);
    }
    result.nodes = searcher.nodes;
    result.elapsed = elapsed();
    return result;
}

} // namespace regicide
