#include "regicide/evaluation.hpp"

#include <algorithm>
#include <array>
#include <bit>
#include <cstdint>

namespace regicide {
namespace {
constexpr int full_phase = 24;

enum class PieceKind : unsigned char { pawn, knight, bishop, rook, queen, king, none };

struct PieceInfo {
    int material = 0; // Signed from White's perspective, outside the phase blend.
    unsigned char phase = 0;
    PieceKind kind = PieceKind::none;
    bool is_white = false;
    unsigned char bonus_index = 0;
};

constexpr auto make_piece_info() {
    std::array<PieceInfo, 256> result{};
    result['P'] = {100, 0, PieceKind::pawn, true, 0};
    result['N'] = {320, 1, PieceKind::knight, true, 1};
    result['B'] = {330, 1, PieceKind::bishop, true, 2};
    result['R'] = {500, 2, PieceKind::rook, true, 3};
    result['Q'] = {900, 4, PieceKind::queen, true, 4};
    result['K'] = {0, 0, PieceKind::king, true, 5};
    result['p'] = {-100, 0, PieceKind::pawn, false, 6};
    result['n'] = {-320, 1, PieceKind::knight, false, 7};
    result['b'] = {-330, 1, PieceKind::bishop, false, 8};
    result['r'] = {-500, 2, PieceKind::rook, false, 9};
    result['q'] = {-900, 4, PieceKind::queen, false, 10};
    result['k'] = {0, 0, PieceKind::king, false, 11};
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

struct ActivitySide {
    std::uint64_t occupied = 0;
    std::uint64_t pawn_attacks = 0;
    unsigned bishop_colors = 0;
};

constexpr auto make_knight_attacks() {
    std::array<std::uint64_t, 64> result{};
    constexpr std::array<std::array<int, 2>, 8> offsets{{
        {{1, 2}}, {{2, 1}}, {{2, -1}}, {{1, -2}},
        {{-1, -2}}, {{-2, -1}}, {{-2, 1}}, {{-1, 2}}}};
    for (int square = 0; square < 64; ++square) {
        for (const auto& offset : offsets) {
            const int file = square % 8 + offset[0];
            const int rank = square / 8 + offset[1];
            if (file >= 0 && file < 8 && rank >= 0 && rank < 8)
                result[square] |= std::uint64_t{1} << (rank * 8 + file);
        }
    }
    return result;
}

constexpr auto make_pawn_attacks() {
    std::array<std::array<std::uint64_t, 64>, 2> result{};
    for (int side = 0; side < 2; ++side) {
        for (int square = 0; square < 64; ++square) {
            const int rank = square / 8 + (side == 0 ? 1 : -1);
            if (rank < 0 || rank >= 8) continue;
            for (int delta : {-1, 1}) {
                const int file = square % 8 + delta;
                if (file >= 0 && file < 8)
                    result[side][square] |= std::uint64_t{1} << (rank * 8 + file);
            }
        }
    }
    return result;
}

constexpr auto knight_attacks = make_knight_attacks();
constexpr auto pawn_attacks = make_pawn_attacks();

// This is bounded pseudo-mobility, not legal move generation: friendly pieces
// and enemy pawn attacks are excluded, but pins and other enemy attacks are not.
// A capturable enemy blocker counts once and stops a ray. Kings are not scored.
int activity_mobility(const Position& position, int square, PieceKind kind,
                      std::uint64_t excluded) {
    if (kind == PieceKind::knight)
        return std::popcount(knight_attacks[square] & ~excluded);
    constexpr std::array<std::array<int, 2>, 8> directions{{
        {{1, 1}}, {{1, -1}}, {{-1, 1}}, {{-1, -1}},
        {{1, 0}}, {{-1, 0}}, {{0, 1}}, {{0, -1}}}};
    const int begin = kind == PieceKind::rook ? 4 : 0;
    const int end = kind == PieceKind::bishop ? 4 : 8;
    int count = 0;
    for (int direction = begin; direction < end; ++direction) {
        const auto& step = directions[direction];
        int file = square % 8 + step[0], rank = square / 8 + step[1];
        while (file >= 0 && file < 8 && rank >= 0 && rank < 8) {
            const int target = rank * 8 + file;
            count += (excluded & (std::uint64_t{1} << target)) == 0;
            if (position.squares[target] != '.') break;
            file += step[0];
            rank += step[1];
        }
    }
    return count;
}

struct ActivityScore {
    int middlegame = 0;
    int endgame = 0;
};

ActivityScore piece_activity_score(const Position& position,
                                  const std::array<ActivitySide, 2>& sides,
                                  const std::array<ShieldInfo, 2>& pawns) {
    // Original conservative starting weights, not trained or imported weights.
    constexpr std::array<int, 5> neutral{0, 4, 7, 7, 14};
    constexpr std::array<int, 5> mg_weight{0, 3, 3, 2, 1};
    constexpr std::array<int, 5> eg_weight{0, 2, 3, 3, 1};
    ActivityScore result;
    for (int side = 0; side < 2; ++side) {
        // Two bishops on the same square color do not constitute the pair.
        if (sides[side].bishop_colors == 3) {
            const int sign = side == 0 ? 1 : -1;
            result.middlegame += sign * 20;
            result.endgame += sign * 30;
        }
    }
    for (int square = 0; square < 64; ++square) {
        const auto& info = piece_info[static_cast<unsigned char>(position.squares[square])];
        if (info.kind < PieceKind::knight || info.kind > PieceKind::queen) continue;
        const int side = info.is_white ? 0 : 1, sign = info.is_white ? 1 : -1;
        const auto index = static_cast<std::size_t>(info.kind);
        const auto excluded = sides[side].occupied | sides[1 - side].pawn_attacks;
        const int mobility = activity_mobility(position, square, info.kind, excluded) - neutral[index];
        result.middlegame += sign * mobility * mg_weight[index];
        result.endgame += sign * mobility * eg_weight[index];
        if (info.kind == PieceKind::rook && !pawns[side].pawn_ranks[square % 8]) {
            const bool open = !pawns[1 - side].pawn_ranks[square % 8];
            result.middlegame += sign * (open ? 18 : 10);
            result.endgame += sign * (open ? 12 : 8);
        }
    }
    return result;
}

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
// coefficient is stored separately from the fixed square bonuses. The lookup
// below bakes color, actual board square and score sign into the same formulas.
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
    std::array<std::array<Bonus, 64>, 12> result{};
    for (std::size_t index = 0; index < result.size(); ++index) {
        const bool is_white = index < 6;
        const int sign = is_white ? 1 : -1;
        const auto kind = static_cast<PieceKind>(index % 6);
        for (int square = 0; square < 64; ++square) {
            const int rank = is_white ? square / 8 : 7 - square / 8;
            const auto bonus = position_bonus(kind, square % 8, rank);
            result[index][square] = {sign * bonus.middlegame, sign * bonus.endgame,
                                     sign * bonus.undeveloped_coefficient};
        }
    }
    return result;
}

constexpr auto position_bonuses = make_position_bonuses();

constexpr bool valid_piece_info() {
    for (const auto& info : piece_info) {
        if (info.kind == PieceKind::none) continue;
        const auto index = static_cast<std::size_t>(info.bonus_index);
        const auto expected = static_cast<std::size_t>(info.kind) + (info.is_white ? 0U : 6U);
        if (index >= position_bonuses.size() || index != expected) return false;
    }
    return true;
}

static_assert(valid_piece_info());
static_assert(sizeof(PieceInfo) == 8);
static_assert(sizeof(PawnMaskInfo) == 8);
static_assert(pawn_masks[255].doubled_middlegame == -56 &&
              pawn_masks[1].enemy_ahead == 127 && pawn_masks[128].enemy_ahead == 0);
static_assert(sizeof(piece_info) + sizeof(position_bonuses) + sizeof(pawn_masks) +
              sizeof(passed_middlegame) + sizeof(passed_endgame) <= 15 * 1024);
} // namespace

int evaluate_position(const Board& board, bool king_pawn_shield, bool pawn_structure,
                      bool piece_activity) {
    const auto& position = board.position();
    const int white_home = home_minors(position, true);
    const int black_home = home_minors(position, false);
    int material_score = 0, middlegame = 0, endgame = 0, phase = 0;
    std::array<ShieldInfo, 2> shields;
    std::array<ActivitySide, 2> activity;
    for (int square = 0; square < 64; ++square) {
        const char piece = position.squares[square];
        if (piece == '.') continue;
        const auto& info = piece_info[static_cast<unsigned char>(piece)];
        if (info.kind == PieceKind::none) continue;
        const bool is_white = info.is_white;
        const auto& bonus = position_bonuses[info.bonus_index][square];
        material_score += info.material;
        middlegame += bonus.middlegame - bonus.undeveloped_coefficient *
                                            (is_white ? white_home : black_home);
        endgame += bonus.endgame;
        const int weight = info.phase;
        phase += weight;
        if ((king_pawn_shield || pawn_structure || piece_activity) && info.kind == PieceKind::pawn) {
            const int rank = is_white ? square / 8 : 7 - square / 8;
            shields[is_white ? 0 : 1].pawn_ranks[square % 8] |= 1U << rank;
        }
        if (piece_activity) {
            const int side = is_white ? 0 : 1;
            activity[side].occupied |= std::uint64_t{1} << square;
            if (info.kind == PieceKind::pawn)
                activity[side].pawn_attacks |= pawn_attacks[side][square];
            if (info.kind == PieceKind::bishop)
                activity[side].bishop_colors |= 1U << ((square / 8 + square % 8) % 2);
        }
        if (king_pawn_shield) {
            auto& shield = shields[is_white ? 0 : 1];
            shield.phase += weight;
            if (info.kind == PieceKind::king) {
                shield.king_file = square % 8;
                shield.king_rank = is_white ? square / 8 : 7 - square / 8;
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
    if (piece_activity) {
        const auto score = piece_activity_score(position, activity, shields);
        middlegame += score.middlegame;
        endgame += score.endgame;
    }
    // Clamp promotions above opening material. Blend the color differences
    // before integer division so reflected positions have exact antisymmetry.
    phase = std::min(phase, full_phase);
    const int score = material_score +
        (middlegame * phase + endgame * (full_phase - phase)) / full_phase;
    return position.turn == Color::white ? score : -score;
}

} // namespace regicide
