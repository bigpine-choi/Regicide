#include "reference_evaluation_v054.hpp"

#include <algorithm>
#include <array>
#include <cctype>

namespace regicide {
namespace {
constexpr int full_phase = 24;

char type(char piece) {
    return static_cast<char>(std::tolower(static_cast<unsigned char>(piece)));
}

bool white(char piece) { return piece >= 'A' && piece <= 'Z'; }

int material(char piece) {
    switch (type(piece)) {
    case 'p': return 100;
    case 'n': return 320;
    case 'b': return 330;
    case 'r': return 500;
    case 'q': return 900;
    default: return 0;
    }
}

int phase_weight(char piece) {
    switch (type(piece)) {
    case 'n': case 'b': return 1;
    case 'r': return 2;
    case 'q': return 4;
    default: return 0;
    }
}

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

struct Bonus { int middlegame; int endgame; };

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
// No learned weights or third-party piece-square tables are used.
Bonus position_bonus(char piece, int file, int rank, int undeveloped) {
    const int file_center = std::min(file, 7 - file);
    const int rank_center = std::min(rank, 7 - rank);
    const int center = file_center + rank_center;
    switch (type(piece)) {
    case 'p': {
        const int advanced = std::max(0, rank - 3);
        return {4 * rank + 3 * file_center +
                    (rank >= 2 && rank <= 4 ? 3 * file_center : 0),
                8 * rank + 2 * file_center + 4 * advanced * advanced};
    }
    case 'n': return {6 * center + (rank > 0 ? 12 : 0), 4 * center};
    case 'b': return {4 * center + (rank > 0 ? 10 : 0), 4 * center};
    case 'r': return {2 * file_center + (rank == 6 ? 18 : 0) -
                         (rank > 0 && rank < 5 ? 3 * undeveloped : 0),
                     2 * file_center + (rank == 6 ? 20 : 0)};
    case 'q': return {2 * center - (rank > 0 ? 4 * undeveloped : 0), 3 * center};
    case 'k': return {-7 * center - 5 * rank +
                         (rank == 0 && (file <= 2 || file >= 6) ? 25 : 0),
                     8 * center};
    default: return {0, 0};
    }
}
} // namespace

int evaluate_position_v054(const Board& board, bool king_pawn_shield) {
    const auto& position = board.position();
    const int white_home = home_minors(position, true);
    const int black_home = home_minors(position, false);
    int material_score = 0, middlegame = 0, endgame = 0, phase = 0;
    std::array<ShieldInfo, 2> shields;
    for (int square = 0; square < 64; ++square) {
        const char piece = position.squares[square];
        if (piece == '.') continue;
        const bool is_white = white(piece);
        const int sign = is_white ? 1 : -1;
        const int rank = is_white ? square / 8 : 7 - square / 8;
        const Bonus bonus = position_bonus(piece, square % 8, rank,
                                           is_white ? white_home : black_home);
        material_score += sign * material(piece);
        middlegame += sign * bonus.middlegame;
        endgame += sign * bonus.endgame;
        const int weight = phase_weight(piece);
        phase += weight;
        if (king_pawn_shield) {
            auto& shield = shields[is_white ? 0 : 1];
            shield.phase += weight;
            if (type(piece) == 'p') shield.pawn_ranks[square % 8] |= 1U << rank;
            else if (type(piece) == 'k') {
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
