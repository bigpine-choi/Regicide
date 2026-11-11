#include "regicide/mate.hpp"

#include <algorithm>
#include <stdexcept>
#include <utility>

namespace regicide {
namespace {
using Clock = std::chrono::steady_clock;

struct Proof {
    MateStatus status = MateStatus::no_mate_within_bound;
    std::vector<Move> pv;
};

class Solver {
public:
    Solver(const Board& board, const MateLimits& limits, const std::atomic_bool* stop)
        : board_(board), limits_(limits), stop_(stop),
          attacker_(board.position().turn), started_(Clock::now()) {}

    MateResult run() {
        auto legal = board_.legal_moves();
        if (limits_.root_moves) {
            if (limits_.root_moves->empty())
                throw std::invalid_argument("mate search requires nonempty root moves");
            for (const auto move : *limits_.root_moves) {
                if (std::find(legal.begin(), legal.end(), move) == legal.end())
                    throw std::invalid_argument("illegal mate search root move");
                if (std::count(limits_.root_moves->begin(), limits_.root_moves->end(), move) != 1)
                    throw std::invalid_argument("duplicate mate search root move");
            }
            restrict_root(legal);
        }
        if (board_.outcome() != Outcome::ongoing) {
            result_.status = MateStatus::no_mate_within_bound;
            result_.reason = "terminal_root";
            return finish();
        }
        result_.best_move = legal.front();
        for (int moves = 1; moves <= limits_.moves; ++moves) {
            const auto proof = visit(2 * moves - 1, true);
            if (proof.status == MateStatus::unknown) {
                result_.status = MateStatus::unknown;
                result_.reason = interrupted_;
                return finish();
            }
            result_.completed_moves = moves;
            if (proof.status == MateStatus::proven) {
                result_.status = MateStatus::proven;
                result_.pv = proof.pv;
                result_.mate_plies = static_cast<int>(proof.pv.size());
                result_.best_move = proof.pv.front();
                result_.reason = "all_defenses_proven";
                return finish();
            }
        }
        result_.status = MateStatus::no_mate_within_bound;
        result_.reason = "bound_exhausted";
        return finish();
    }

private:
    void restrict_root(std::vector<Move>& legal) const {
        legal.erase(std::remove_if(legal.begin(), legal.end(), [this](Move move) {
            return std::find(limits_.root_moves->begin(), limits_.root_moves->end(), move)
                == limits_.root_moves->end();
        }), legal.end());
    }

    bool interrupted() {
        if (stop_ && stop_->load(std::memory_order_relaxed)) interrupted_ = "stopped";
        else if (limits_.nodes && result_.nodes >= limits_.nodes) interrupted_ = "node_limit";
        else if (limits_.movetime.count() > 0 && Clock::now() - started_ >= limits_.movetime)
            interrupted_ = "time_limit";
        return !interrupted_.empty();
    }

    Proof visit(int remaining, bool root = false) {
        if (interrupted()) return {MateStatus::unknown, {}};
        ++result_.nodes;
        // Board outcome prioritizes checkmate over automatic move-count draws.
        const auto outcome = board_.outcome();
        if (outcome == Outcome::checkmate)
            return {board_.position().turn != attacker_ ? MateStatus::proven
                : MateStatus::no_mate_within_bound, {}};
        if (outcome != Outcome::ongoing) return {};
        const bool attack = board_.position().turn == attacker_;
        if (!attack) {
            const auto claims = board_.claims_now();
            if (claims.threefold || claims.fifty_moves) return {};
        }
        if (remaining == 0) return {};
        auto legal = board_.legal_moves();
        if (root && limits_.root_moves) restrict_root(legal);
        // Ordering only: retain every quiet and special move.
        std::stable_partition(legal.begin(), legal.end(),
            [this](Move move) { return board_.gives_check_generated(move); });
        Proof longest{MateStatus::proven, {}};
        for (const auto move : legal) {
            if (interrupted()) return {MateStatus::unknown, {}};
            if (!attack && (board_.position().halfmove >= 99 || board_.has_repetition_candidate())) {
                const auto claims = board_.claims_after(move);
                if (claims.threefold || claims.fifty_moves) return {};
            }
            board_.play_generated(move);
            auto child = visit(remaining - 1);
            board_.undo();
            // Conservative interruption: never use an unfinished subtree as refutation.
            if (child.status == MateStatus::unknown) return child;
            if (attack && child.status == MateStatus::proven) {
                child.pv.insert(child.pv.begin(), move);
                return child;
            }
            if (!attack && child.status == MateStatus::no_mate_within_bound) return {};
            if (!attack && child.pv.size() + 1 > longest.pv.size()) {
                child.pv.insert(child.pv.begin(), move);
                longest = std::move(child);
            }
        }
        return attack ? Proof{} : longest;
    }

    MateResult finish() {
        result_.elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - started_);
        return result_;
    }

    Board board_;
    const MateLimits& limits_;
    const std::atomic_bool* stop_;
    Color attacker_;
    Clock::time_point started_;
    MateResult result_;
    std::string interrupted_;
};
} // namespace

MateResult solve_mate(const Board& board, const MateLimits& limits, const std::atomic_bool* stop) {
    if (limits.moves < 1 || limits.moves > 64)
        throw std::invalid_argument("mate moves must be 1..64");
    if (limits.movetime.count() < 0)
        throw std::invalid_argument("mate movetime must not be negative");
    return Solver(board, limits, stop).run();
}
} // namespace regicide
