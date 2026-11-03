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

constexpr bool inside(int file, int rank) {
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

struct SquareList {
    std::array<unsigned char, 8> squares{};
    unsigned char count = 0;
};
struct AttackGeometry {
    std::array<SquareList, 2> pawns{};
    SquareList knights{};
    std::array<SquareList, 8> rays{};
};

// Occupancy changes during an exchange; geometry does not. These small tables
// remove repeated file/rank arithmetic and edge tests from both attack queries.
constexpr auto attack_geometry = [] {
    std::array<AttackGeometry, 64> result{};
    for (int target = 0; target < 64; ++target) {
        auto& geometry = result[target];
        const int file = target % 8, rank = target / 8;
        for (unsigned side = 0; side < 2; ++side) {
            const int pawn_rank = rank + (side == 0 ? -1 : 1);
            for (int delta : {-1, 1})
                if (inside(file + delta, pawn_rank))
                    geometry.pawns[side].squares[geometry.pawns[side].count++] =
                        static_cast<unsigned char>(pawn_rank * 8 + file + delta);
        }
        for (const auto& step : knight_steps) {
            const int f = file + step[0], r = rank + step[1];
            if (inside(f, r)) geometry.knights.squares[geometry.knights.count++] =
                static_cast<unsigned char>(r * 8 + f);
        }
        for (int d = 0; d < 8; ++d) {
            auto& ray = geometry.rays[d];
            for (int f = file + directions[d][0], r = rank + directions[d][1];
                 inside(f, r); f += directions[d][0], r += directions[d][1])
                ray.squares[ray.count++] = static_cast<unsigned char>(r * 8 + f);
        }
    }
    return result;
}();

struct ExchangePosition {
    std::array<char, 64> squares;
    std::array<int, 2> kings{};
};

// Target-centered geometry discovers sliding x-rays after every occupancy
// change. Pin restrictions are applied separately to recaptures; geometric
// attacks, including pinned pieces, determine whether a king is safe.
template<class Accept>
bool visit_attackers(const ExchangePosition& position, int target, Color side, Accept&& accept) {
    const auto& geometry = attack_geometry[target];
    const auto& pawns = geometry.pawns[color_index(side)];
    for (unsigned i = 0; i < pawns.count; ++i) {
        const int from = pawns.squares[i];
        if (position.squares[from] == colored('p', side) && accept(from)) return true;
    }
    for (unsigned i = 0; i < geometry.knights.count; ++i) {
        const int from = geometry.knights.squares[i];
        if (position.squares[from] == colored('n', side) && accept(from)) return true;
    }
    for (int d = 0; d < 8; ++d) {
        const auto& ray = geometry.rays[d];
        for (unsigned distance = 0; distance < ray.count; ++distance) {
            const int from = ray.squares[distance];
            const char piece = position.squares[from];
            if (piece == '.') continue;
            if (belongs(piece, side)) {
                const char t = type(piece);
                if (t == 'q' || (d < 4 && t == 'r') || (d >= 4 && t == 'b') ||
                    (distance == 0 && t == 'k'))
                    if (accept(from)) return true;
            }
            break;
        }
    }
    return false;
}

// On success commits precisely one legal LVA recapture and returns its
// promotion increment. Equal-valued attackers use the lower square index.
bool recapture(ExchangePosition& position, int target, Color side, int& promotion_gain) {
    // At most two pawn, eight knight and eight first-on-ray attackers exist.
    // Only initialized entries are read. Usually the first legal LVA succeeds,
    // so selecting its minimum avoids sorting attackers that will not be used.
    std::array<int, 18> candidates;
    int count = 0;
    visit_attackers(position, target, side, [&](int from) {
        candidates[count++] = from;
        return false;
    });
    while (count > 0) {
        int least = 0;
        for (int i = 1; i < count; ++i) {
            const int a = value(position.squares[candidates[i]]);
            const int b = value(position.squares[candidates[least]]);
            if (a < b || (a == b && candidates[i] < candidates[least])) least = i;
        }
        const int from = candidates[least];
        candidates[least] = candidates[--count];
        const char piece = position.squares[from], victim = position.squares[target];
        const unsigned king_index = color_index(side);
        const int old_king = position.kings[king_index];
        const bool promotion = type(piece) == 'p' && (target / 8 == 0 || target / 8 == 7);
        position.squares[from] = '.';
        // In this local material model queen promotion maximizes immediate
        // gain if retained; if recaptured its extra gain and loss cancel.
        position.squares[target] = promotion ? colored('q', side) : piece;
        if (type(piece) == 'k') position.kings[king_index] = target;
        // King safety needs one geometric attack, including pinned attackers,
        // rather than the complete collection used for choosing a recapture.
        const bool safe = !visit_attackers(position, position.kings[king_index], opposite(side),
                                          [](int) { return true; });
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
    std::array<int, 64> gain;
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
