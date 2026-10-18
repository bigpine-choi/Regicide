#include "regicide/evaluation.hpp"

#include <algorithm>
#include <array>
#include <cstdint>

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

struct PawnScore {
    int middlegame = 0;
    int endgame = 0;
};

struct PawnMaskInfo {
    std::int8_t doubled_middlegame = 0;
    std::int8_t doubled_endgame = 0;
    std::int8_t isolated_middlegame = 0;
    std::int8_t isolated_endgame = 0;
    std::uint8_t passed_middlegame = 0;
    std::uint8_t passed_endgame = 0;
    std::uint8_t enemy_ahead = 0;
    std::uint8_t next_white_rank = 0;
};

constexpr std::array<int, 8> passed_middlegame{0, 0, 5, 10, 20, 35, 60, 0};
constexpr std::array<int, 8> passed_endgame{0, 0, 10, 20, 40, 70, 120, 0};

constexpr auto make_pawn_masks() {
    std::array<PawnMaskInfo, 256> result{};
    for (unsigned mask = 1; mask < result.size(); ++mask) {
        int count = 0, front = 0;
        for (int rank = 0; rank < 8; ++rank) {
            if (!(mask & (1U << rank))) continue;
            ++count;
            front = rank;
        }
        auto& info = result[mask];
        info.doubled_middlegame = static_cast<std::int8_t>(-(count - 1) * 8);
        info.doubled_endgame = static_cast<std::int8_t>(-(count - 1) * 12);
        info.isolated_middlegame = static_cast<std::int8_t>(-count * 10);
        info.isolated_endgame = static_cast<std::int8_t>(-count * 12);
        info.passed_middlegame = static_cast<std::uint8_t>(passed_middlegame[front]);
        info.passed_endgame = static_cast<std::uint8_t>(passed_endgame[front]);
        // In the opponent's coordinates, ranks below 7-front are ahead of us.
        info.enemy_ahead = static_cast<std::uint8_t>((1U << (7 - front)) - 1);
        info.next_white_rank = static_cast<std::uint8_t>((front + 1) * 8);
    }
    return result;
}

constexpr auto pawn_masks = make_pawn_masks();

PawnScore pawn_structure_score(const Position& position, const ShieldInfo& own,
                               const ShieldInfo& enemy, bool is_white) {
    PawnScore result;
    for (int file = 0; file < 8; ++file) {
        const unsigned mask = own.pawn_ranks[file];
        if (!mask) continue;
        const auto& info = pawn_masks[mask];
        const bool isolated = (file == 0 || !own.pawn_ranks[file - 1]) &&
                              (file == 7 || !own.pawn_ranks[file + 1]);
        result.middlegame += info.doubled_middlegame +
                            (isolated ? info.isolated_middlegame : 0);
        result.endgame += info.doubled_endgame + (isolated ? info.isolated_endgame : 0);

        // The foremost pawn's zero bonuses include ranks 0, 1 and 7, so these
        // ranks need neither enemy inspection nor a forward-square lookup.
        if (!info.passed_endgame) continue;
        unsigned opposing = enemy.pawn_ranks[file];
        if (file > 0) opposing |= enemy.pawn_ranks[file - 1];
        if (file < 7) opposing |= enemy.pawn_ranks[file + 1];
        // Keep enemy ranks in their own coordinates; no reversal lookup.
        if (opposing & info.enemy_ahead) continue;
        const int next_rank = is_white ? info.next_white_rank : 56 - info.next_white_rank;
        const unsigned blocked = static_cast<unsigned>(position.squares[next_rank + file] != '.');
        // The nonnegative bonuses make a one-bit shift exactly the old /2 rule.
        result.middlegame += info.passed_middlegame >> blocked;
        result.endgame += info.passed_endgame >> blocked;
    }
    return result;
}

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
static_assert(sizeof(PawnMaskInfo) == 8);
static_assert(pawn_masks[255].doubled_middlegame == -56 &&
              pawn_masks[1].enemy_ahead == 127 && pawn_masks[128].enemy_ahead == 0);
static_assert(sizeof(piece_info) + sizeof(position_bonuses) + sizeof(pawn_masks) +
              sizeof(passed_middlegame) + sizeof(passed_endgame) <= 15 * 1024);
} // namespace

int evaluate_position(const Board& board, bool king_pawn_shield, bool pawn_structure) {
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
        if ((king_pawn_shield || pawn_structure) && info.kind == PieceKind::pawn)
            shields[is_white ? 0 : 1].pawn_ranks[square % 8] |= 1U << rank;
        if (king_pawn_shield) {
            auto& shield = shields[is_white ? 0 : 1];
            shield.phase += weight;
            if (info.kind == PieceKind::king) {
                shield.king_file = square % 8;
                shield.king_rank = rank;
            }
        }
    }
    if (king_pawn_shield) {
        middlegame += pawn_shield_penalty(shields[1], shields[0].phase) -
                      pawn_shield_penalty(shields[0], shields[1].phase);
    }
    if (pawn_structure) {
        const auto white = pawn_structure_score(position, shields[0], shields[1], true);
        const auto black = pawn_structure_score(position, shields[1], shields[0], false);
        middlegame += white.middlegame - black.middlegame;
        endgame += white.endgame - black.endgame;
    }
    // Clamp promotions above opening material. Blend the color differences
    // before integer division so reflected positions have exact antisymmetry.
    phase = std::min(phase, full_phase);
    const int score = material_score +
        (middlegame * phase + endgame * (full_phase - phase)) / full_phase;
    return position.turn == Color::white ? score : -score;
}

} // namespace regicide
