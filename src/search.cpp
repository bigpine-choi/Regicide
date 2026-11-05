#include "regicide/search.hpp"
#include "regicide/neural.hpp"
#include "regicide/nnue.hpp"
#include "regicide/evaluation.hpp"
#include "regicide/see.hpp"
#include "regicide/transposition.hpp"

#include <algorithm>
#include <array>
#include <bit>
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

constexpr int knight_steps[8][2] = {
    {1, 2}, {2, 1}, {2, -1}, {1, -2}, {-1, -2}, {-2, -1}, {-2, 1}, {-1, 2}};

bool belongs_to(char piece, Color side) {
    return piece != '.' && ((piece >= 'A' && piece <= 'Z') == (side == Color::white));
}

// A bounded horizon trigger. Queen exchanges alone do not force a reply;
// pawn, minor, rook and king attacks do. Pinned attackers may also trigger
// the search: only the subsequent legal replies determine the score. Never
// force the queen itself to move (a sacrifice or counterattack can be best).
bool queen_under_lesser_attack(const Board& board) {
    const auto& position = board.position();
    const Color enemy = opposite(position.turn);
    const char queen = position.turn == Color::white ? 'Q' : 'q';
    const auto enemy_piece = [&](int file, int rank, char type) {
        if (file < 0 || file >= 8 || rank < 0 || rank >= 8) return false;
        const char piece = position.squares[rank * 8 + file];
        return belongs_to(piece, enemy) && (piece == type || piece == type - ('a' - 'A'));
    };
    constexpr int rays[8][2] = {
        {1, 0}, {-1, 0}, {0, 1}, {0, -1}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}};
    for (int square = 0; square < 64; ++square) {
        if (position.squares[square] != queen) continue;
        const int file = square % 8, rank = square / 8;
        const int pawn_rank = rank + (enemy == Color::white ? -1 : 1);
        if (enemy_piece(file - 1, pawn_rank, 'p') || enemy_piece(file + 1, pawn_rank, 'p')) return true;
        for (const auto& step : knight_steps)
            if (enemy_piece(file + step[0], rank + step[1], 'n')) return true;
        for (int direction = 0; direction < 8; ++direction) {
            for (int distance = 1; distance < 8; ++distance) {
                const int f = file + rays[direction][0] * distance;
                const int r = rank + rays[direction][1] * distance;
                if (f < 0 || f >= 8 || r < 0 || r >= 8) break;
                if (position.squares[r * 8 + f] == '.') continue;
                if (enemy_piece(f, r, direction < 4 ? 'r' : 'b') ||
                    (distance == 1 && enemy_piece(f, r, 'k'))) return true;
                break;
            }
        }
    }
    return false;
}

// A conservative motif, not an evaluation bonus or a claim that a fork wins:
// a quiet knight move attacks two enemy rooks/queens, or threatens a legal,
// locally non-losing capture which attacks two such pieces. The actual search
// must then allow all opponent replies, including quiet prevention and capture
// of the knight. Hypothetical legality/SEE use a private board, never history or
// NNUE state belonging to the search. The classifier changes no search score.
template<class Poll>
bool quiet_knight_fork_threat(const Board& board, Move move, std::uint64_t& see_calls, Poll&& poll) {
    const auto& before = board.position();
    if (before.squares[move.from] != 'N' && before.squares[move.from] != 'n') return false;
    auto squares = before.squares;
    squares[move.to] = squares[move.from];
    squares[move.from] = '.';
    const Color enemy = opposite(before.turn);
    const auto majors = [&](int square) {
        int count = 0;
        for (const auto& step : knight_steps) {
            const int file = square % 8 + step[0], rank = square / 8 + step[1];
            if (file < 0 || file >= 8 || rank < 0 || rank >= 8) continue;
            const char piece = squares[rank * 8 + file];
            if (belongs_to(piece, enemy) && value(piece) >= 500) ++count;
        }
        return count;
    };
    if (majors(move.to) >= 2) return true;
    std::vector<Move> captures;
    for (const auto& step : knight_steps) {
        const int file = move.to % 8 + step[0], rank = move.to / 8 + step[1];
        if (file < 0 || file >= 8 || rank < 0 || rank >= 8) continue;
        const int target = rank * 8 + file;
        const char victim = squares[target];
        if (belongs_to(victim, enemy) && victim != 'K' && victim != 'k' && majors(target) >= 2)
            captures.push_back({move.to, target});
    }
    if (captures.empty()) return false;
    poll();
    Board after = board;
    after.play_generated(move);
    auto hypothetical_fen = after.fen();
    hypothetical_fen[hypothetical_fen.find(' ') + 1] = before.turn == Color::white ? 'w' : 'b';
    Board hypothetical(hypothetical_fen);
    const auto legal = hypothetical.legal_moves();
    for (Move capture : captures) {
        poll();
        if (std::find(legal.begin(), legal.end(), capture) == legal.end()) continue;
        ++see_calls;
        if (static_exchange_evaluation(hypothetical, capture) >= 0) return true;
    }
    return false;
}

// A candidate generator, never an evaluation bonus. A quiet rook lift that
// newly controls two squares around an exposed king merits one full reply
// search. All legal defenses, including captures and counterchecks, survive.
bool quiet_rook_pressure(const Board& board, Move move) {
    const auto& before = board.position();
    const bool white = before.turn == Color::white;
    if (before.squares[move.from] != (white ? 'R' : 'r') || before.squares[move.to] != '.') return false;
    const char enemy_king = white ? 'k' : 'K';
    int king = -1;
    for (int square = 0; square < 64; ++square)
        if (before.squares[square] == enemy_king) { king = square; break; }
    if (king < 0 || king / 8 < 2 || king / 8 > 5) return false;
    std::uint64_t zone = 0;
    for (int dr = -1; dr <= 1; ++dr) for (int df = -1; df <= 1; ++df) {
        const int r = king / 8 + dr, f = king % 8 + df;
        if ((dr || df) && r >= 0 && r < 8 && f >= 0 && f < 8)
            zone |= UINT64_C(1) << (r * 8 + f);
    }
    const auto attacks = [](const auto& squares, int from) {
        std::uint64_t mask = 0;
        for (const auto& [df, dr] : std::array<std::pair<int, int>, 4>{{{1,0}, {-1,0}, {0,1}, {0,-1}}}) {
            int f = from % 8 + df, r = from / 8 + dr;
            while (f >= 0 && f < 8 && r >= 0 && r < 8) {
                mask |= UINT64_C(1) << (r * 8 + f);
                if (squares[r * 8 + f] != '.') break;
                f += df; r += dr;
            }
        }
        return mask;
    };
    auto after = before.squares;
    after[move.to] = after[move.from]; after[move.from] = '.';
    const auto added = attacks(after, move.to) & zone & ~attacks(before.squares, move.from);
    return std::popcount(added) >= 2;
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
    AppliedMove(Board& board, Move move, bool fast, NeuralAccumulator* nnue = nullptr,
                std::vector<Move>* observed_path = nullptr)
        : board_(board), nnue_(nnue), observed_path_(observed_path) {
        if (nnue_) {
            const Position before = board_.position();
            saved_ = *nnue_;
            if (fast) board_.play_generated(move);
            else board_.play(move);
            // No allocation or exception can separate board publication from
            // accumulator publication; failed Board::play leaves sums intact.
            nnue_->update_generated(before, board_.position(), move);
        } else {
            if (fast) board_.play_generated(move);
            else board_.play(move);
        }
        // Searcher reserves the full legal search-ply bound before recursion.
        if (observed_path_) observed_path_->push_back(move);
    }
    ~AppliedMove() {
        board_.undo();
        if (nnue_) *nnue_ = *saved_;
        if (observed_path_) observed_path_->pop_back();
    }
    AppliedMove(const AppliedMove&) = delete;
    AppliedMove& operator=(const AppliedMove&) = delete;
private:
    Board& board_;
    NeuralAccumulator* nnue_;
    std::optional<NeuralAccumulator> saved_;
    std::vector<Move>* observed_path_;
};

// Keep both pre-existing evaluator meanings: positional=false selects material
// for classical/residual evaluation, while explicit integer NNUE is independent.
int static_score(const Board& board, const SearchOptions& options,
                 const NeuralAccumulator* accumulator = nullptr) {
    if (options.nnue) {
        const auto& model = options.nnue_model ? *options.nnue_model : builtin_neural_model();
        return accumulator ? accumulator->evaluate(board.position().turn) : evaluate_neural(board, model);
    }
    return options.positional ? (options.neural ? options.neural->evaluate(
               board, options.king_pawn_shield, options.pawn_structure) :
               evaluate_position(board, options.king_pawn_shield, options.pawn_structure,
                                 options.piece_activity)) :
               evaluate_material(board);
}

struct Interrupted {};
struct ForcingLimit {};

// Normal-node bounds cannot describe a quiescence query: optional quiet checks,
// fork preparation, mandatory defense and the absolute q ceiling all change its
// candidate tree. Keep every one of those contexts in a separate cache key,
// along with the existing draw-aware position/history/counter key.
struct QuiescenceKey {
    TranspositionKey board;
    int remaining = -1;
    int checks = 0;
    int threats = 0;
    bool defensive = false;
    int forcing_owner = -1;
    int forcing_left = 0;
    bool cycle_refresh = false;
    bool forcing_enabled = false;
    bool operator==(const QuiescenceKey&) const = default;
};

struct QuiescenceEntry {
    QuiescenceKey key;
    int score = 0; // Root-distance mates use the existing TT normalization.
    TranspositionBound bound = TranspositionBound::exact;
    std::array<std::uint16_t, max_quiescence_depth> pv{};
    std::uint8_t pv_size = 0;
};

constexpr std::size_t quiescence_cache_limit = 1024 * 1024;

// A full bounded q PV fits in 64 bytes. Never publish only a cached first move:
// tactical data collection and users must be able to replay its entire witness.
std::uint16_t encode_qmove(Move move) {
    const unsigned promotion = move.promotion == 'q' ? 1U : move.promotion == 'r' ? 2U :
                               move.promotion == 'b' ? 3U : move.promotion == 'n' ? 4U : 0U;
    return static_cast<std::uint16_t>(static_cast<unsigned>(move.from) |
        (static_cast<unsigned>(move.to) << 6) | (promotion << 12));
}

Move decode_qmove(std::uint16_t encoded) {
    constexpr std::array<char, 5> promotion{{0, 'q', 'r', 'b', 'n'}};
    return {encoded & 63, (encoded >> 6) & 63, promotion[encoded >> 12]};
}

class Searcher {
public:
    Searcher(const SearchLimits& limits, const std::atomic_bool* stop, Clock::time_point start,
             SearchOptions options, TranspositionTable* table, const Board& root)
        : limits_(limits), stop_(stop), start_(start), options_(options), table_(table) {
        if (options_.nnue && options_.nnue_incremental)
            nnue_.emplace(root, options_.nnue_model ? *options_.nnue_model : builtin_neural_model());
        if (options_.static_evaluation_observer) observed_path_.reserve(max_search_ply + 1);
        if (options_.quiescence && options_.quiescence_cache)
            qcache_.resize(quiescence_cache_limit / sizeof(QuiescenceEntry));
    }

    std::uint64_t nodes = 0;
    std::uint64_t qnodes = 0;
    std::uint64_t tt_probes = 0, tt_hits = 0, tt_cutoffs = 0, tt_stores = 0;
    std::uint64_t pvs_probes = 0, pvs_researches = 0;
    std::uint64_t see_calls = 0;
    std::uint64_t lmr_reductions = 0, lmr_researches = 0;
    std::uint64_t quiet_threat_candidates = 0, quiet_threat_extensions = 0, defensive_qnodes = 0;
    std::uint64_t qsee_pruned = 0;
    std::uint64_t qcache_probes = 0, qcache_hits = 0, qcache_cutoffs = 0, qcache_stores = 0;
    std::uint64_t root_threat_candidates = 0, root_threat_searches = 0;
    std::uint64_t queen_defense_qnodes = 0;
    std::uint64_t forcing_checks = 0;
    std::uint64_t forcing_fallbacks = 0;
    std::uint64_t qcache_bytes() const { return qcache_.size() * sizeof(QuiescenceEntry); }
    int seldepth = 0;
    int iteration_depth = 0;

    int negamax(Board& board, int depth, int ply, int alpha, int beta,
                std::vector<Move>& pv, bool pv_node,
                const std::optional<Move>& preferred = {}) {
        // A horizon position belongs to exactly one node, even when extended.
        if (depth == 0 && options_.quiescence) {
            if (options_.quiescence_forcing) {
                // An unresolved extended tree is not a completed score. Bound
                // its work locally and re-search this same frontier with the
                // original policy. All spent nodes still count globally.
                try {
                    return quiescence(board, ply, 0, options_.quiescence_checks ? 1 : 0,
                        1, false, alpha, beta, pv,
                        board.in_check() ? (board.position().turn == Color::white ? 1 : 0) : -1,
                        2, true, true, nodes + 65536);
                } catch (const ForcingLimit&) { ++forcing_fallbacks; }
            }
            return quiescence(board, ply, 0, options_.quiescence_checks ? 1 : 0,
                options_.quiescence_threats ? 1 : 0, false, alpha, beta, pv, -1, 0, false, false, 0);
        }
        poll();
        ++nodes;
        seldepth = std::max(seldepth, ply);
        pv.clear();
        auto moves = board.legal_moves();
        // A mate on the last searched ply outranks material and draw counters.
        if (moves.empty()) return board.in_check() ? -mate_score + ply : 0;
        if (automatic_draw(board)) return 0;
        if (ply == 0 && options_.root_moves) {
            std::erase_if(moves, [this](Move move) {
                return std::find(options_.root_moves->begin(), options_.root_moves->end(), move) ==
                       options_.root_moves->end();
            });
        }
        const bool current_claim = claimable(board);
        if (depth == 0) {
            int raw_score;
            const int score = static_evaluation(board, raw_score);
            const bool adjusted = score < 0 && (current_claim || intended_claim_available(board, moves));
            observe_evaluation(board, raw_score, score, ply, -1, current_claim, adjusted);
            if (adjusted) return 0;
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
        // Reductions belong only to non-PV sibling scouts. Keep every root,
        // principal-variation node, check evasion and current draw claim full.
        const bool lmr_node = options_.lmr && options_.pvs && options_.quiet_ordering &&
                              !pv_node && ply > 0 && depth >= 4 && beta == alpha + 1 &&
                              !current_claim && !board.in_check();
        const auto color = board.position().turn == Color::white ? 0U : 1U;
        if (options_.see_ordering)
            order_moves_with_see(board, moves, ordered_preferred,
                                 options_.quiet_ordering ? &killers_[ply] : nullptr,
                                 options_.quiet_ordering ? &history_[color] : nullptr,
                                 see_calls, [this] { poll(false); });
        else if (options_.quiet_ordering)
            order_moves(board, moves, ordered_preferred, &killers_[ply], &history_[color]);
        else order_moves(board, moves, ordered_preferred);
        std::vector<Move> root_threats;
        if (options_.tactical_threat_ordering && ply == 0 && depth == 1) {
            // A forcing quiet preparation can establish alpha before expensive
            // capture/check frontiers. Classify the same general motif used by
            // qsearch; keep every legal move and the existing within-class order.
            // Only the first root iteration changes. Completed-PV ordering and
            // every descendant keep their original policies, including LMR.
            for (Move move : moves) {
                poll(false);
                if (quiet_move(board, move) && !board.gives_check_generated(move) &&
                    quiet_knight_fork_threat(board, move, see_calls, [this] { poll(false); })) {
                    root_threats.push_back(move);
                    ++root_threat_candidates;
                }
            }
            std::stable_partition(moves.begin(), moves.end(), [&](Move move) {
                return std::find(root_threats.begin(), root_threats.end(), move) != root_threats.end();
            });
        }
        int best = -infinity;
        bool first_move = true;
        std::size_t move_index = 0;
        bool reduced_fail_low = false;
        for (Move move : moves) {
            poll(false);
            if (std::find(root_threats.begin(), root_threats.end(), move) != root_threats.end())
                ++root_threat_searches;
            int score;
            bool unverified_reduction = false;
            std::vector<Move> child_pv;
            bool reduce_candidate = false;
            if (lmr_node && move_index >= 4 && alpha > -mate_score + max_search_ply &&
                beta < mate_score - max_search_ply) {
                const char piece = board.position().squares[move.from];
                reduce_candidate = piece != 'P' && piece != 'p' && piece != 'K' && piece != 'k' &&
                    quiet_move(board, move) && (!preferred || move != *preferred) &&
                    (!ordered_preferred || move != *ordered_preferred) &&
                    killers_[ply][0] != move && killers_[ply][1] != move;
            }
            {
                AppliedMove applied(board, move, options_.fast_moves, nnue_ ? &*nnue_ : nullptr,
                                    options_.static_evaluation_observer ? &observed_path_ : nullptr);
                const bool intended_claim = claimable(board);
                if (options_.pvs && !first_move) {
                    // A one-point window first asks only whether this move can
                    // improve alpha. An improvement below beta needs a normal
                    // search before publishing its score or principal variation.
                    ++pvs_probes;
                    if (reduce_candidate && !intended_claim && !board.in_check()) {
                        ++lmr_reductions;
                        // The reduced child receives its actual depth, so any
                        // child TT entry can advertise only that searched depth.
                        score = -negamax(board, depth - 2, ply + 1, -alpha - 1, -alpha,
                                         child_pv, false);
                        if (score > alpha) {
                            ++lmr_researches;
                            ++pvs_probes;
                            score = -negamax(board, depth - 1, ply + 1, -alpha - 1, -alpha,
                                             child_pv, false);
                        } else {
                            // A shallow fail-low is only a decision not to
                            // improve alpha. It is never an exact move score or
                            // a principal variation, even if above old best.
                            unverified_reduction = true;
                        }
                    } else {
                        score = -negamax(board, depth - 1, ply + 1, -alpha - 1, -alpha,
                                         child_pv, false);
                    }
                    // The mover's intended claim belongs to this move's value,
                    // so account for it before deciding whether to re-search.
                    if (intended_claim) score = std::max(0, score);
                    if (score > alpha && score < beta) {
                        ++pvs_researches;
                        score = -negamax(board, depth - 1, ply + 1, -beta, -alpha, child_pv, pv_node);
                    }
                } else {
                    score = -negamax(board, depth - 1, ply + 1, -beta, -alpha, child_pv, pv_node);
                }
                // The mover may claim before an intended move that creates a
                // third repetition or completes 50 moves, even if the opponent
                // would decline a claim and continue with a winning position.
                if (intended_claim) score = std::max(0, score);
            }
            first_move = false;
            ++move_index;
            if (unverified_reduction) {
                reduced_fail_low = true;
                continue;
            }
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
        // A non-PV fail-low with unverified late moves supplies alpha as an
        // upper bound, not their shallow scores. The retained PV starts with a
        // full-depth searched move. A verified cutoff is unaffected by earlier
        // reduced siblings because that full-depth move proves the lower bound.
        const bool selective_upper = reduced_fail_low && best < beta;
        const int score = selective_upper ? alpha : current_claim ? std::max(0, best) : best;
        // Selective fail-low mixes horizons. Even a shallower TT depth would
        // not make this a proven fixed-horizon bound, so do not cache it.
        // A restricted root proves nothing about excluded moves. Never let
        // that value masquerade as a full-position TT bound at a descendant.
        if (table_ && !selective_upper && !(ply == 0 && options_.root_moves)) {
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
    std::optional<NeuralAccumulator> nnue_;
    std::array<Killers, max_search_ply + 1> killers_{};
    std::array<QuietHistory, 2> history_{}; // Bounded, reset for every search.
    std::vector<Move> observed_path_;
    std::uint64_t observed_evaluations_ = 0;
    // A new Searcher is constructed for every search, including when the caller
    // reuses SearchContext. No score, PV, model or option state survives a call.
    std::vector<QuiescenceEntry> qcache_;

    std::size_t qcache_index(const QuiescenceKey& key) const {
        std::uint64_t mixed = key.board.position ^
            (key.board.repetition_context * UINT64_C(0x9e3779b97f4a7c15));
        mixed ^= static_cast<std::uint64_t>(key.board.halfmove) * UINT64_C(0xbf58476d1ce4e5b9);
        mixed ^= static_cast<std::uint64_t>(key.remaining + 1) * UINT64_C(0x94d049bb133111eb);
        mixed ^= static_cast<std::uint64_t>(key.checks + 4 * key.threats + 16 * key.defensive);
        mixed ^= static_cast<std::uint64_t>(key.forcing_owner + 1 + 4 * key.forcing_left +
                                          32 * key.cycle_refresh + 64 * key.forcing_enabled) *
                 UINT64_C(0xd6e8feb86659fd93);
        mixed ^= mixed >> 30;
        mixed *= UINT64_C(0xbf58476d1ce4e5b9);
        mixed ^= mixed >> 27;
        return static_cast<std::size_t>(mixed % qcache_.size());
    }

    std::optional<int> qcache_probe(const QuiescenceKey& key, int ply, int alpha, int beta,
                                    std::vector<Move>& pv) {
        if (qcache_.empty()) return {};
        ++qcache_probes;
        const auto& entry = qcache_[qcache_index(key)];
        if (entry.key != key) return {};
        ++qcache_hits;
        const int score = score_from_table(entry.score, ply);
        if (entry.bound != TranspositionBound::exact &&
            !(entry.bound == TranspositionBound::lower && score >= beta) &&
            !(entry.bound == TranspositionBound::upper && score <= alpha)) return {};
        ++qcache_cutoffs;
        for (std::size_t i = 0; i < entry.pv_size; ++i) pv.push_back(decode_qmove(entry.pv[i]));
        return score;
    }

    void qcache_store(const QuiescenceKey& key, int ply, int score,
                      TranspositionBound bound, const std::vector<Move>& pv) {
        if (qcache_.empty()) return;
        // The complete PV must fit the query's absolute extension ceiling.
        // A defensive/check node at that ceiling throws instead of storing.
        if (pv.size() > static_cast<std::size_t>(key.remaining))
            throw std::logic_error("quiescence cache PV exceeds remaining depth");
        auto& entry = qcache_[qcache_index(key)];
        if (entry.key == key && entry.bound == TranspositionBound::exact &&
            bound != TranspositionBound::exact) return;
        entry.key = key;
        entry.score = score_to_table(score, ply);
        entry.bound = bound;
        entry.pv_size = static_cast<std::uint8_t>(pv.size());
        for (std::size_t i = 0; i < pv.size(); ++i) entry.pv[i] = encode_qmove(pv[i]);
        ++qcache_stores;
    }

    int static_evaluation(const Board& board, int& raw_score) const {
        raw_score = static_score(board, options_, nnue_ ? &*nnue_ : nullptr);
        const int reserved_plies = options_.quiescence ? max_search_ply : max_search_depth;
        return std::clamp(raw_score, -mate_score + reserved_plies + 1,
                          mate_score - reserved_plies - 1);
    }

    void observe_evaluation(const Board& board, int raw_score, int score, int ply, int qply,
                            bool current_claim, bool adjusted) {
        if (options_.static_evaluation_observer)
            options_.static_evaluation_observer(board, observed_path_,
                {raw_score, score, iteration_depth, ply, qply, ++observed_evaluations_,
                 current_claim, adjusted});
    }

    int quiescence(Board& board, int ply, int qply, int checks_left, int threats_left,
                   bool defensive_reply, int alpha, int beta, std::vector<Move>& pv,
                   int forcing_owner, int forcing_left, bool cycle_refresh,
                   bool forcing_enabled, std::uint64_t forcing_end) {
        poll();
        if (forcing_enabled && nodes >= forcing_end) throw ForcingLimit{};
        ++nodes;
        ++qnodes;
        if (defensive_reply) ++defensive_qnodes;
        seldepth = std::max(seldepth, ply);
        pv.clear();
        const int original_alpha = alpha;
        const QuiescenceKey key{{board.position_key(), board.repetition_context_key(),
                                  board.position().halfmove},
                                 max_quiescence_depth - qply, checks_left, threats_left, defensive_reply,
                                 forcing_owner, forcing_left, cycle_refresh, forcing_enabled};
        if (const auto cached = qcache_probe(key, ply, alpha, beta, pv)) return *cached;
        const auto finish = [&](int score, bool exact = false) {
            const auto bound = exact ? TranspositionBound::exact :
                score <= original_alpha ? TranspositionBound::upper :
                score >= beta ? TranspositionBound::lower : TranspositionBound::exact;
            qcache_store(key, ply, score, bound, pv);
            return score;
        };
        auto moves = board.legal_moves();
        const bool checked = board.in_check();
        // Repeated checks warrant more calculation, not an artificial draw
        // score. Only the existing current/intended claim rules can grant 0.
        // A single refill bounds the extension even when a player declines a
        // draw. The incoming budget and permission are part of the cache key.
        if (cycle_refresh && checked && board.repetition_count() >= 2) {
            forcing_left += 2;
            cycle_refresh = false;
        }
        // Continue only a narrow forcing attack. Keep every legal evasion,
        // but do not spend extra checks chasing a king with many escapes.
        // The incoming owner remains in the cache key; this deterministic
        // normalization is derived from this exact position, not its window.
        if (checked && moves.size() > 3) forcing_owner = -1;
        const int mover = board.position().turn == Color::white ? 0 : 1;
        const bool continuing = forcing_left > 0 && forcing_owner == mover;
        if (moves.empty()) return finish(checked ? -mate_score + ply : 0, true);
        if (automatic_draw(board)) return finish(0, true);
        const bool current_claim = claimable(board);
        // No position in check may use stand pat. If the absolute extension
        // ceiling is reached, restart an optional forcing frontier with the
        // baseline policy, or discard an incomplete baseline iteration. Never
        // treat an unresolved check as quiet.
        if ((checked || defensive_reply) && qply >= max_quiescence_depth) {
            if (forcing_enabled) throw ForcingLimit{};
            throw Interrupted{};
        }

        // Only the first q ply can open this full-width defense. Descendants
        // resume the existing capture/check policy, so this cannot recursively
        // expand a quiet queen chase. qply is already part of the q-cache key;
        // the option is immutable during the lifetime of this search's caches.
        const bool queen_defense = options_.quiescence_queen_defense && qply == 0 &&
            !checked && !defensive_reply && queen_under_lesser_attack(board);
        if (queen_defense) ++queen_defense_qnodes;

        int best = -infinity;
        std::vector<Move> threats;
        const bool optional_quiet = !options_.quiescence_horizon_budget || qply <= 2;
        if (!checked && !defensive_reply && !queen_defense) {
            // An unused optional extension must not make a long capture chain
            // launch a second full-width tree arbitrarily far from the normal
            // horizon. Keep the first three q plies, including a preparation
            // after two captures. Mandatory defenses/check evasions bypass this
            // block; checking captures, EP and promotions are never filtered by
            // this horizon policy. The q cache already keys exact remaining
            // depth (= max_quiescence_depth - qply), and the policy is immutable
            // for the lifetime of its search-local cache.
            int raw_score;
            best = static_evaluation(board, raw_score);
            // A quiet intended move can permit a draw even though quiescence
            // otherwise searches only captures and promotions.
            const bool adjusted = best < 0 && (current_claim || intended_claim_available(board, moves));
            observe_evaluation(board, raw_score, best, ply, qply, current_claim, adjusted);
            if (adjusted) best = 0;
            if (best >= beta || qply >= max_quiescence_depth) return finish(best);
            alpha = std::max(alpha, best);
            std::erase_if(moves, [&](Move move) {
                if (!quiet_move(board, move)) {
                    // Local SEE is a selective speed heuristic, not a proof of
                    // tactical worth. Keep forced replies and all promotions,
                    // en passant and checking captures, including sacrifices.
                    // This opt-in policy is independent of SEE move ordering.
                    const auto& position = board.position();
                    const char piece = position.squares[move.from];
                    const bool ep = (piece == 'P' || piece == 'p') && move.to == position.en_passant;
                    if (options_.quiescence_threats && !move.promotion && !ep &&
                        !board.gives_check_generated(move)) {
                        poll(false);
                        ++see_calls;
                        if (static_exchange_evaluation(board, move) < 0) {
                            ++qsee_pruned;
                            return true;
                        }
                    }
                    return false;
                }
                if ((checks_left > 0 || threats_left > 0 || continuing) && board.gives_check_generated(move))
                    return !(optional_quiet && checks_left > 0) && !continuing;
                if (threats_left > 0 && qply + 2 < max_quiescence_depth &&
                    ((options_.quiescence_threats && optional_quiet &&
                      quiet_knight_fork_threat(board, move, see_calls, [this] { poll(false); })) ||
                     (forcing_enabled && (forcing_owner == mover || qply == 0) &&
                      quiet_rook_pressure(board, move)))) {
                    threats.push_back(move);
                    ++quiet_threat_candidates;
                    return false;
                }
                return true;
            });
        } else if (current_claim) {
            best = 0;
            alpha = std::max(alpha, 0);
            if (alpha >= beta) return finish(0);
        }
        if (options_.see_ordering)
            order_moves_with_see(board, moves, {}, nullptr, nullptr, see_calls, [this] { poll(false); });
        else order_moves(board, moves);
        for (Move move : moves) {
            poll(false);
            int score;
            const bool quiet = quiet_move(board, move);
            const bool threat = std::find(threats.begin(), threats.end(), move) != threats.end();
            if (threat) ++quiet_threat_extensions;
            std::vector<Move> child_pv;
            {
                AppliedMove applied(board, move, options_.fast_moves, nnue_ ? &*nnue_ : nullptr,
                                    options_.static_evaluation_observer ? &observed_path_ : nullptr);
                const bool intended_claim = claimable(board);
                // Captures/promotions preserve the quiet-check budget. All
                // check evasions remain legal candidates even at zero; a
                // quiet countercheck consumes the budget only if one remains.
                const int child_checks = checks_left -
                    (checks_left > 0 && quiet && board.in_check() ? 1 : 0);
                const bool gives_check = board.in_check();
                // Mandatory evasions/counterchecks never need an extension
                // credit. A quiet move by the attacker ends this chain; a
                // defender's evasion leaves the attacker its next forcing turn.
                const bool extra_check = !checked && !defensive_reply && !queen_defense &&
                    quiet && gives_check && !(optional_quiet && checks_left > 0) && continuing;
                if (extra_check) ++forcing_checks;
                const int child_owner = !forcing_enabled ? -1 :
                    gives_check || threat ? mover : (forcing_owner == mover ? -1 : forcing_owner);
                score = -quiescence(board, ply + 1, qply + 1, child_checks,
                                    threats_left - (threat ? 1 : 0), threat,
                                    -beta, -alpha, child_pv, child_owner,
                                    forcing_left - (extra_check ? 1 : 0), cycle_refresh,
                                    forcing_enabled, forcing_end);
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
        return finish(best);
    }

    bool intended_claim_available(Board& board, const std::vector<Move>& moves) {
        // A third occurrence is impossible unless some reversible-history
        // position has already occurred twice. The board maintains this fact
        // exactly, avoiding a full speculative move scan at most q leaves.
        if (board.position().halfmove < 99 && !board.has_repetition_candidate()) return false;
        for (Move move : moves) {
            poll(false);
            AppliedMove applied(board, move, options_.fast_moves, nnue_ ? &*nnue_ : nullptr);
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
    if (options.neural && options.nnue)
        throw std::invalid_argument("residual/replacement neural and integer NNUE cannot be enabled together");
    const auto start = Clock::now();
    SearchResult result;
    Board board = input;
    auto moves = board.legal_moves();
    if (options.root_moves) {
        if (options.root_moves->empty())
            throw std::invalid_argument("root move list must be nonempty");
        std::vector<Move> seen;
        for (Move move : *options.root_moves) {
            if (std::find(moves.begin(), moves.end(), move) == moves.end())
                throw std::invalid_argument("illegal root move: " + move.uci());
            if (std::find(seen.begin(), seen.end(), move) != seen.end())
                throw std::invalid_argument("duplicate root move: " + move.uci());
            seen.push_back(move);
        }
    }
    const auto elapsed = [&] {
        return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start);
    };
    if (moves.empty() || automatic_draw(board)) {
        result.score = moves.empty() && board.in_check() ? -mate_score : 0;
        result.elapsed = elapsed();
        return result;
    }
    if (options.root_moves) {
        std::erase_if(moves, [&options](Move move) {
            return std::find(options.root_moves->begin(), options.root_moves->end(), move) ==
                   options.root_moves->end();
        });
    }
    order_moves(board, moves);
    result.best_move = moves.front();
    result.pv.push_back(moves.front());
    result.score = static_score(board, options);
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
    Searcher searcher(limits, stop, start, options, table, board);
    for (int depth = 1; depth <= limits.depth; ++depth) {
        searcher.iteration_depth = depth;
        std::vector<Move> pv;
        int score;
        try {
            // Before the first iteration, best_move is only a cheap legal
            // cancellation fallback, not a searched PV. Let SEE choose that
            // iteration's first candidate; later completed PVs remain first.
            // The disabled path preserves the previous version's exact order.
            const auto preferred = depth == 1 && options.see_ordering ?
                                   std::optional<Move>{} : result.best_move;
            score = searcher.negamax(board, depth, 0, -infinity, infinity, pv, true, preferred);
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
        result.lmr_reductions = searcher.lmr_reductions;
        result.lmr_researches = searcher.lmr_researches;
        result.quiet_threat_candidates = searcher.quiet_threat_candidates;
        result.quiet_threat_extensions = searcher.quiet_threat_extensions;
        result.defensive_qnodes = searcher.defensive_qnodes;
        result.qsee_pruned = searcher.qsee_pruned;
        result.qcache_probes = searcher.qcache_probes;
        result.qcache_hits = searcher.qcache_hits;
        result.qcache_cutoffs = searcher.qcache_cutoffs;
        result.qcache_stores = searcher.qcache_stores;
        result.qcache_bytes = searcher.qcache_bytes();
        result.root_threat_candidates = searcher.root_threat_candidates;
        result.root_threat_searches = searcher.root_threat_searches;
        result.queen_defense_qnodes = searcher.queen_defense_qnodes;
        result.forcing_checks = searcher.forcing_checks;
        result.forcing_fallbacks = searcher.forcing_fallbacks;
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
    result.lmr_reductions = searcher.lmr_reductions;
    result.lmr_researches = searcher.lmr_researches;
    result.quiet_threat_candidates = searcher.quiet_threat_candidates;
    result.quiet_threat_extensions = searcher.quiet_threat_extensions;
    result.defensive_qnodes = searcher.defensive_qnodes;
    result.qsee_pruned = searcher.qsee_pruned;
    result.qcache_probes = searcher.qcache_probes;
    result.qcache_hits = searcher.qcache_hits;
    result.qcache_cutoffs = searcher.qcache_cutoffs;
    result.qcache_stores = searcher.qcache_stores;
    result.qcache_bytes = searcher.qcache_bytes();
    result.root_threat_candidates = searcher.root_threat_candidates;
    result.root_threat_searches = searcher.root_threat_searches;
    result.queen_defense_qnodes = searcher.queen_defense_qnodes;
    result.forcing_checks = searcher.forcing_checks;
    result.forcing_fallbacks = searcher.forcing_fallbacks;
    result.seldepth = searcher.seldepth;
    result.elapsed = elapsed();
    return result;
}

} // namespace regicide
