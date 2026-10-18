#include "regicide/evaluation.hpp"

#include <algorithm>
#include <array>

namespace regicide {
namespace {
constexpr int full_phase = 24;

enum class PieceKind : unsigned char { pawn, knight, bishop, rook, queen, king, none };

struct PieceInfo {
    int material = 0;
    unsigned char phase = 0;
    PieceKind kind = PieceKind::none;
    bool is_white = false;
};

constexpr auto make_piece_info() {
    std::array<PieceInfo, 256> result{};
    result['P'] = {100, 0, PieceKind::pawn, true};
    result['N'] = {320, 1, PieceKind::knight, true};
    result['B'] = {330, 1, PieceKind::bishop, true};
    result['R'] = {500, 2, PieceKind::rook, true};
    result['Q'] = {900, 4, PieceKind::queen, true};
    result['K'] = {0, 0, PieceKind::king, true};
    result['p'] = {100, 0, PieceKind::pawn, false};
    result['n'] = {320, 1, PieceKind::knight, false};
    result['b'] = {330, 1, PieceKind::bishop, false};
    result['r'] = {500, 2, PieceKind::rook, false};
    result['q'] = {900, 4, PieceKind::queen, false};
    result['k'] = {0, 0, PieceKind::king, false};
    return result;
}

constexpr auto piece_info = make_piece_info();

// Counts the original home squares still occupied by that color's minor type.
// This is a position feature, not a penalty based on move or repetition history.
int home_minors(const Position& position, bool is_white) {
    const int home_rank = is_white ? 0 : 56;
    const char knight = is_white ? 'N' : 'n';
    const char bishop = is_white ? 'B' : 'b';
    return static_cast<int>(position.squares[home_rank + 1] == knight) +
           static_cast<int>(position.squares[home_rank + 6] == knight) +
           static_cast<int>(position.squares[home_rank + 2] == bishop) +
           static_cast<int>(position.squares[home_rank + 5] == bishop);
}

struct Bonus {
    int middlegame;
    int endgame;
    int undeveloped_coefficient;
};

struct ShieldInfo {
    // Bits use the owner's relative ranks: bit zero is the home rank.
    std::array<unsigned, 8> pawn_ranks{};
    int king_file = -1;
    int king_rank = 0;
    int phase = 0;
};

int pawn_shield_penalty(const ShieldInfo& defender, int attacking_phase) {
    if (!attacking_phase || defender.king_file < 0) return 0;
    int penalty = 0;
    for (int file = std::max(0, defender.king_file - 1);
         file <= std::min(7, defender.king_file + 1); ++file) {
        // Only pawns strictly ahead of the current king can shelter it. The
        // nearest such pawn costs 0/4/8 cp at distances 1/2/3, then 12 cp.
        const unsigned ahead = defender.pawn_ranks[file] >> (defender.king_rank + 1);
        if (ahead & 1U) continue;
        penalty += (ahead & 2U) ? 4 : (ahead & 4U) ? 8 : 12;
    }
    // This original, conservative heuristic estimates shelter, not attacks.
    // Scale by the opponent's remaining pieces; pawn-only opposition costs 0.
    return penalty * std::min(attacking_phase, full_phase / 2) / (full_phase / 2);
}

// These small formulas are Regicide's own initial heuristics. Files/ranks are
// relative to the owner: rank zero is home, centrality is 0..3 on each axis.
// Evaluate them at compile time; no learned or third-party tables are used.
// Only the home-minor count varies with the rest of the board, so its linear
// coefficient is stored separately from the fixed square bonuses.
constexpr Bonus position_bonus(PieceKind kind, int file, int rank) {
    const int file_center = std::min(file, 7 - file);
    const int rank_center = std::min(rank, 7 - rank);
    const int center = file_center + rank_center;
    switch (kind) {
    case PieceKind::pawn: {
        const int advanced = std::max(0, rank - 3);
        return {4 * rank + 3 * file_center +
                    (rank >= 2 && rank <= 4 ? 3 * file_center : 0),
                8 * rank + 2 * file_center + 4 * advanced * advanced, 0};
    }
    case PieceKind::knight: return {6 * center + (rank > 0 ? 12 : 0), 4 * center, 0};
    case PieceKind::bishop: return {4 * center + (rank > 0 ? 10 : 0), 4 * center, 0};
    case PieceKind::rook: return {2 * file_center + (rank == 6 ? 18 : 0),
                                 2 * file_center + (rank == 6 ? 20 : 0),
                                 rank > 0 && rank < 5 ? 3 : 0};
    case PieceKind::queen: return {2 * center, 3 * center, rank > 0 ? 4 : 0};
    case PieceKind::king: return {-7 * center - 5 * rank +
                                     (rank == 0 && (file <= 2 || file >= 6) ? 25 : 0),
                                 8 * center, 0};
    default: return {0, 0, 0};
    }
}

constexpr auto make_position_bonuses() {
    std::array<std::array<Bonus, 64>, 6> result{};
    for (std::size_t kind = 0; kind < result.size(); ++kind)
        for (int square = 0; square < 64; ++square)
            result[kind][square] = position_bonus(static_cast<PieceKind>(kind),
                                                 square % 8, square / 8);
    return result;
}

constexpr auto position_bonuses = make_position_bonuses();

constexpr bool valid_piece_info() {
    for (const auto& info : piece_info)
        if (info.kind != PieceKind::none &&
            static_cast<std::size_t>(info.kind) >= position_bonuses.size()) return false;
    return true;
}

static_assert(valid_piece_info());
static_assert(sizeof(piece_info) + sizeof(position_bonuses) <= 15 * 1024);
} // namespace

int evaluate_position(const Board& board, bool king_pawn_shield) {
    const auto& position = board.position();
    const int white_home = home_minors(position, true);
    const int black_home = home_minors(position, false);
    int material_score = 0, middlegame = 0, endgame = 0, phase = 0;
    std::array<ShieldInfo, 2> shields;
    for (int square = 0; square < 64; ++square) {
        const char piece = position.squares[square];
        if (piece == '.') continue;
        const auto& info = piece_info[static_cast<unsigned char>(piece)];
        if (info.kind == PieceKind::none) continue;
        const bool is_white = info.is_white;
        const int sign = is_white ? 1 : -1;
        const int rank = is_white ? square / 8 : 7 - square / 8;
        const auto& bonus = position_bonuses[static_cast<std::size_t>(info.kind)]
                                            [rank * 8 + square % 8];
        material_score += sign * info.material;
        middlegame += sign * (bonus.middlegame - bonus.undeveloped_coefficient *
                                                   (is_white ? white_home : black_home));
        endgame += sign * bonus.endgame;
        const int weight = info.phase;
        phase += weight;
        if (king_pawn_shield) {
            auto& shield = shields[is_white ? 0 : 1];
            shield.phase += weight;
            if (info.kind == PieceKind::pawn) shield.pawn_ranks[square % 8] |= 1U << rank;
            else if (info.kind == PieceKind::king) {
                shield.king_file = square % 8;
                shield.king_rank = rank;
            }
        }
    }
    if (king_pawn_shield) {
        middlegame += pawn_shield_penalty(shields[1], shields[0].phase) -
                      pawn_shield_penalty(shields[0], shields[1].phase);
    }
    // Clamp promotions above opening material. Blend the color differences
    // before integer division so reflected positions have exact antisymmetry.
    phase = std::min(phase, full_phase);
    const int score = material_score +
        (middlegame * phase + endgame * (full_phase - phase)) / full_phase;
    return position.turn == Color::white ? score : -score;
}

} // namespace regicide
