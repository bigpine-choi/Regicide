#include "regicide/see.hpp"

#include <algorithm>
#include <array>
#include <cassert>

namespace regicide {
namespace {
constexpr int knight_steps[8][2] = {
    {1, 2}, {2, 1}, {2, -1}, {1, -2}, {-1, -2}, {-2, -1}, {-2, 1}, {-1, 2}};
constexpr int directions[8][2] = {
    {1, 0}, {-1, 0}, {0, 1}, {0, -1}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}};

bool inside(int file, int rank) {
    return file >= 0 && file < 8 && rank >= 0 && rank < 8;
}
char type(char piece) {
    return piece >= 'A' && piece <= 'Z' ? static_cast<char>(piece + ('a' - 'A')) : piece;
}
bool belongs(char piece, Color side) {
    return piece != '.' && ((piece >= 'A' && piece <= 'Z') == (side == Color::white));
}
char colored(char piece, Color side) {
    return side == Color::white ? static_cast<char>(piece - ('a' - 'A')) : piece;
}
int value(char piece) {
    switch (type(piece)) {
    case 'p': return 100;
    case 'n': return 320;
    case 'b': return 330;
    case 'r': return 500;
    case 'q': return 900;
    // Kings are considered only after every material attacker. They are never
    // captured: a legal king recapture ends the exchange sequence.
    case 'k': return 20000;
    default: return 0;
    }
}
unsigned color_index(Color side) { return side == Color::white ? 0U : 1U; }

struct ExchangePosition {
    std::array<char, 64> squares;
    std::array<int, 2> kings{};
};

// Target-centered geometry discovers sliding x-rays after every occupancy
// change. Pin restrictions are applied separately to recaptures; geometric
// attacks, including pinned pieces, determine whether a king is safe.
int attackers(const ExchangePosition& position, int target, Color side,
              std::array<int, 64>& result) {
    int count = 0;
    const int file = target % 8, rank = target / 8;
    const int pawn_rank = rank + (side == Color::white ? -1 : 1);
    for (int delta : {-1, 1}) {
        if (!inside(file + delta, pawn_rank)) continue;
        const int from = pawn_rank * 8 + file + delta;
        if (position.squares[from] == colored('p', side)) result[count++] = from;
    }
    for (const auto& step : knight_steps) {
        const int f = file + step[0], r = rank + step[1];
        if (inside(f, r) && position.squares[r * 8 + f] == colored('n', side))
            result[count++] = r * 8 + f;
    }
    for (int d = 0; d < 8; ++d) {
        int f = file + directions[d][0], r = rank + directions[d][1], distance = 1;
        for (; inside(f, r); f += directions[d][0], r += directions[d][1], ++distance) {
            const int from = r * 8 + f;
            const char piece = position.squares[from];
            if (piece == '.') continue;
            if (belongs(piece, side)) {
                const char t = type(piece);
                if (t == 'q' || (d < 4 && t == 'r') || (d >= 4 && t == 'b') ||
                    (distance == 1 && t == 'k')) result[count++] = from;
            }
            break;
        }
    }
    return count;
}

// On success commits precisely one legal LVA recapture and returns its
// promotion increment. Equal-valued attackers use the lower square index.
bool recapture(ExchangePosition& position, int target, Color side, int& promotion_gain) {
    std::array<int, 64> candidates{};
    const int count = attackers(position, target, side, candidates);
    std::sort(candidates.begin(), candidates.begin() + count, [&](int left, int right) {
        const int a = value(position.squares[left]), b = value(position.squares[right]);
        return a != b ? a < b : left < right;
    });
    for (int i = 0; i < count; ++i) {
        const int from = candidates[i];
        const char piece = position.squares[from], victim = position.squares[target];
        const unsigned king_index = color_index(side);
        const int old_king = position.kings[king_index];
        const bool promotion = type(piece) == 'p' && (target / 8 == 0 || target / 8 == 7);
        position.squares[from] = '.';
        // In this local material model queen promotion maximizes immediate
        // gain if retained; if recaptured its extra gain and loss cancel.
        position.squares[target] = promotion ? colored('q', side) : piece;
        if (type(piece) == 'k') position.kings[king_index] = target;
        std::array<int, 64> king_attackers{};
        const bool safe = attackers(position, position.kings[king_index], opposite(side), king_attackers) == 0;
        if (safe) {
            promotion_gain = promotion ? value('q') - value('p') : 0;
            return true;
        }
        position.squares[from] = piece;
        position.squares[target] = victim;
        position.kings[king_index] = old_king;
    }
    return false;
}
} // namespace

int static_exchange_evaluation(const Board& board, Move move) {
    const auto& original = board.position();
    assert(move.from >= 0 && move.from < 64 && move.to >= 0 && move.to < 64);
    const char piece = original.squares[move.from];
    assert(belongs(piece, original.turn));
    const bool en_passant = type(piece) == 'p' && move.to == original.en_passant &&
                            original.squares[move.to] == '.';
    if (original.squares[move.to] == '.' && !en_passant && !move.promotion) return 0;

    ExchangePosition position{original.squares, {}};
    for (int square = 0; square < 64; ++square) {
        if (position.squares[square] == 'K') position.kings[0] = square;
        else if (position.squares[square] == 'k') position.kings[1] = square;
    }
    std::array<int, 64> gain{};
    gain[0] = en_passant ? value('p') : value(position.squares[move.to]);
    if (en_passant) position.squares[move.to + (original.turn == Color::white ? -8 : 8)] = '.';
    position.squares[move.from] = '.';
    position.squares[move.to] = move.promotion ? colored(move.promotion, original.turn) : piece;
    if (move.promotion) gain[0] += value(move.promotion) - value('p');
    if (type(piece) == 'k') position.kings[color_index(original.turn)] = move.to;

    Color side = opposite(original.turn);
    int depth = 0;
    while (depth + 1 < static_cast<int>(gain.size()) && type(position.squares[move.to]) != 'k') {
        const int captured = value(position.squares[move.to]);
        int promotion_gain = 0;
        if (!recapture(position, move.to, side, promotion_gain)) break;
        ++depth;
        gain[depth] = captured + promotion_gain - gain[depth - 1];
        side = opposite(side);
    }
    while (depth > 0) {
        gain[depth - 1] = -std::max(-gain[depth - 1], gain[depth]);
        --depth;
    }
    return gain[0];
}

} // namespace regicide
