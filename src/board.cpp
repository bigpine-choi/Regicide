#include "regicide/board.hpp"

#include <algorithm>
#include <charconv>
#include <cctype>
#include <cmath>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace regicide {
namespace {
constexpr unsigned WK = 1, WQ = 2, BK = 4, BQ = 8;
constexpr int knight_steps[8][2] = {
    {1,2},{2,1},{2,-1},{1,-2},{-1,-2},{-2,-1},{-2,1},{-1,2}};
constexpr int directions[8][2] = {
    {1,0},{-1,0},{0,1},{0,-1},{1,1},{1,-1},{-1,1},{-1,-1}};
bool inside(int file, int rank) {
    return file >= 0 && file < 8 && rank >= 0 && rank < 8;
}
char type(char piece) { return static_cast<char>(std::tolower(static_cast<unsigned char>(piece))); }
bool belongs(char piece, Color color) {
    return piece != '.' && ((piece >= 'A' && piece <= 'Z') == (color == Color::white));
}
char colored(char piece, Color color) {
    return color == Color::white ? static_cast<char>(std::toupper(static_cast<unsigned char>(piece))) : piece;
}
std::string square_name(int square) {
    return {static_cast<char>('a' + square % 8), static_cast<char>('1' + square / 8)};
}
int read_square(std::string_view text) {
    if (text.size() != 2 || text[0] < 'a' || text[0] > 'h' || text[1] < '1' || text[1] > '8')
        throw std::invalid_argument("invalid square");
    return (text[1] - '1') * 8 + text[0] - 'a';
}
int king_square(const Position& p, Color color) {
    return static_cast<int>(std::find(p.squares.begin(), p.squares.end(), colored('k', color)) - p.squares.begin());
}

// Attack maps are geometric: a pinned piece still attacks king/castling squares.
bool attacked(const Position& p, int target, Color by) {
    const int file = target % 8, rank = target / 8;
    const int pawn_rank = rank + (by == Color::white ? -1 : 1);
    for (int delta : {-1, 1})
        if (inside(file + delta, pawn_rank) &&
            p.squares[pawn_rank * 8 + file + delta] == colored('p', by)) return true;
    for (auto& step : knight_steps) {
        const int f = file + step[0], r = rank + step[1];
        if (inside(f, r) && p.squares[r * 8 + f] == colored('n', by)) return true;
    }
    for (int d = 0; d < 8; ++d) {
        int f = file + directions[d][0], r = rank + directions[d][1], distance = 1;
        for (; inside(f, r); f += directions[d][0], r += directions[d][1], ++distance) {
            const char piece = p.squares[r * 8 + f];
            if (piece == '.') continue;
            if (belongs(piece, by)) {
                const char t = type(piece);
                if (t == 'q' || (d < 4 && t == 'r') || (d >= 4 && t == 'b') ||
                    (distance == 1 && t == 'k')) return true;
            }
            break;
        }
    }
    return false;
}
bool checked(const Position& p, Color color) {
    return attacked(p, king_square(p, color), opposite(color));
}
unsigned rook_right(int square) {
    switch (square) { case 0: return WQ; case 7: return WK; case 56: return BQ; case 63: return BK; default: return 0; }
}

// Private primitive: callers supply a generated candidate, then check king safety.
void apply(Position& p, Move move) {
    const char piece = p.squares[move.from];
    const char captured = p.squares[move.to];
    const bool pawn = type(piece) == 'p';
    const bool ep = pawn && move.to == p.en_passant && captured == '.';
    if (ep) p.squares[move.to + (p.turn == Color::white ? -8 : 8)] = '.';
    p.squares[move.from] = '.';
    p.squares[move.to] = move.promotion ? colored(move.promotion, p.turn) : piece;
    if (type(piece) == 'k') {
        p.castling &= ~(p.turn == Color::white ? WK | WQ : BK | BQ);
        if (std::abs(move.to - move.from) == 2) {
            const int rook_from = move.to > move.from ? move.from + 3 : move.from - 4;
            const int rook_to = (move.from + move.to) / 2;
            p.squares[rook_to] = p.squares[rook_from];
            p.squares[rook_from] = '.';
        }
    }
    if (type(piece) == 'r') p.castling &= ~rook_right(move.from);
    if (type(captured) == 'r') p.castling &= ~rook_right(move.to);
    p.en_passant = pawn && std::abs(move.to - move.from) == 16 ? (move.from + move.to) / 2 : -1;
    p.halfmove = pawn || captured != '.' || ep ? 0 : p.halfmove + 1;
    if (p.turn == Color::black) ++p.fullmove;
    p.turn = opposite(p.turn);
}

std::vector<Move> candidates(const Position& p) {
    std::vector<Move> moves;
    moves.reserve(64);
    auto add = [&](int from, int to) {
        if (type(p.squares[from]) == 'p' && (to / 8 == 0 || to / 8 == 7))
            for (char promotion : {'q','r','b','n'}) moves.push_back({from, to, promotion});
        else moves.push_back({from, to});
    };
    for (int from = 0; from < 64; ++from) {
        const char piece = p.squares[from];
        if (!belongs(piece, p.turn)) continue;
        const char t = type(piece);
        const int file = from % 8, rank = from / 8;
        if (t == 'p') {
            const int dir = p.turn == Color::white ? 1 : -1;
            const int next = from + dir * 8;
            if (inside(file, rank + dir) && p.squares[next] == '.') {
                add(from, next);
                if (rank == (p.turn == Color::white ? 1 : 6) && p.squares[next + dir * 8] == '.')
                    add(from, next + dir * 8);
            }
            for (int df : {-1, 1}) {
                if (!inside(file + df, rank + dir)) continue;
                const int to = next + df;
                if ((belongs(p.squares[to], opposite(p.turn)) && type(p.squares[to]) != 'k') ||
                    (to == p.en_passant && p.squares[to] == '.' &&
                     p.squares[to - dir * 8] == colored('p', opposite(p.turn)))) add(from, to);
            }
        } else if (t == 'n') {
            for (auto& step : knight_steps) {
                const int f = file + step[0], r = rank + step[1];
                if (inside(f, r) && !belongs(p.squares[r * 8 + f], p.turn) && type(p.squares[r * 8 + f]) != 'k')
                    add(from, r * 8 + f);
            }
        } else {
            for (int d = 0; d < 8; ++d) {
                if ((t == 'b' && d < 4) || (t == 'r' && d >= 4)) continue;
                int f = file + directions[d][0], r = rank + directions[d][1];
                for (; inside(f, r); f += directions[d][0], r += directions[d][1]) {
                    const char target = p.squares[r * 8 + f];
                    if (belongs(target, p.turn) || type(target) == 'k') break;
                    add(from, r * 8 + f);
                    if (target != '.' || t == 'k') break;
                }
            }
            if (t != 'k' || checked(p, p.turn)) continue;
            const int home = p.turn == Color::white ? 4 : 60;
            if (from != home) continue;
            for (int sign : {-1, 1}) {
                const unsigned right = p.turn == Color::white ? (sign == 1 ? WK : WQ) : (sign == 1 ? BK : BQ);
                const int rook = home + (sign == 1 ? 3 : -4);
                if (!(p.castling & right) || p.squares[rook] != colored('r', p.turn)) continue;
                bool clear = true;
                for (int s = home + sign; s != rook; s += sign)
                    if (p.squares[s] != '.') clear = false;
                if (!clear) continue;
                Position transit = p;
                transit.squares[home] = '.';
                transit.squares[home + sign] = piece;
                if (!checked(transit, p.turn)) add(home, home + sign * 2);
            }
        }
    }
    return moves;
}
std::vector<Move> legal(Position& p) {
    const Position before = p;
    auto moves = candidates(p);
    moves.erase(std::remove_if(moves.begin(), moves.end(), [&](Move move) {
        apply(p, move);
        const bool illegal = checked(p, before.turn);
        p = before;
        return illegal;
    }), moves.end());
    return moves;
}

std::string to_fen(const Position& p) {
    std::ostringstream out;
    for (int rank = 7; rank >= 0; --rank) {
        int empty = 0;
        for (int file = 0; file < 8; ++file) {
            const char piece = p.squares[rank * 8 + file];
            if (piece == '.') { ++empty; continue; }
            if (empty) { out << empty; empty = 0; }
            out << piece;
        }
        if (empty) out << empty;
        if (rank) out << '/';
    }
    out << (p.turn == Color::white ? " w " : " b ");
    if (!p.castling) out << '-';
    else {
        if (p.castling & WK) out << 'K';
        if (p.castling & WQ) out << 'Q';
        if (p.castling & BK) out << 'k';
        if (p.castling & BQ) out << 'q';
    }
    out << ' ' << (p.en_passant == -1 ? "-" : square_name(p.en_passant));
    out << ' ' << p.halfmove << ' ' << p.fullmove;
    return out.str();
}

std::string repetition_key(Position p) {
    if (p.en_passant != -1) {
        const auto moves = legal(p);
        const bool usable = std::any_of(moves.begin(), moves.end(), [&](Move m) {
            return m.to == p.en_passant && type(p.squares[m.from]) == 'p';
        });
        if (!usable) p.en_passant = -1;
    }
    std::string key = to_fen(p);
    // Counters are not part of position identity.
    key.erase(key.rfind(' '));
    key.erase(key.rfind(' '));
    return key;
}

int counter(std::string_view s, int minimum) {
    int value = 0;
    const auto result = std::from_chars(s.data(), s.data() + s.size(), value);
    // Bound imported counters to leave room for realistic analysis without overflow.
    if (result.ec != std::errc{} || result.ptr != s.data() + s.size() || value < minimum || value > 1000000000)
        throw std::invalid_argument("invalid FEN counter (maximum 1000000000)");
    return value;
}
Position parse_fen(std::string_view text) {
    std::istringstream in{std::string(text)};
    std::string board, side, castle, ep, half, full, extra;
    if (!(in >> board >> side >> castle >> ep >> half >> full) || (in >> extra))
        throw std::invalid_argument("FEN requires exactly six fields");
    Position p;
    p.squares.fill('.');
    int rank = 7, file = 0;
    bool previous_digit = false;
    for (char c : board) {
        if (c == '/') {
            if (file != 8 || rank == 0) throw std::invalid_argument("invalid FEN rank");
            --rank; file = 0; previous_digit = false;
        } else if (c >= '1' && c <= '8') {
            if (previous_digit) throw std::invalid_argument("consecutive FEN empty-square digits");
            previous_digit = true;
            file += c - '0';
            if (file > 8) throw std::invalid_argument("FEN rank exceeds eight squares");
        } else {
            if (std::string_view("PNBRQKpnbrqk").find(c) == std::string_view::npos || file >= 8)
                throw std::invalid_argument("invalid FEN piece");
            if (type(c) == 'p' && (rank == 0 || rank == 7))
                throw std::invalid_argument("pawn on promotion rank");
            p.squares[rank * 8 + file++] = c;
            previous_digit = false;
        }
    }
    if (rank != 0 || file != 8) throw std::invalid_argument("FEN board must have eight complete ranks");
    if (side != "w" && side != "b") throw std::invalid_argument("invalid FEN turn");
    p.turn = side == "w" ? Color::white : Color::black;
    p.halfmove = counter(half, 0);
    p.fullmove = counter(full, 1);
    for (Color color : {Color::white, Color::black}) {
        int kings = 0, pawns = 0, pieces = 0;
        for (char piece : p.squares) if (belongs(piece, color)) {
            ++pieces; kings += type(piece) == 'k'; pawns += type(piece) == 'p';
        }
        if (kings != 1 || pawns > 8 || pieces > 16) throw std::invalid_argument("invalid FEN material counts");
    }
    if (checked(p, opposite(p.turn))) throw std::invalid_argument("side that just moved is in check");
    if (castle != "-") for (char c : castle) {
        unsigned right = 0;
        switch (c) { case 'K': right=WK; break; case 'Q': right=WQ; break;
                     case 'k': right=BK; break; case 'q': right=BQ; break;
                     default: throw std::invalid_argument("invalid FEN castling field"); }
        if (p.castling & right) throw std::invalid_argument("duplicate FEN castling right");
        const bool white = right == WK || right == WQ;
        const int king = white ? 4 : 60;
        const int rook = right == WK ? 7 : right == WQ ? 0 : right == BK ? 63 : 56;
        if (p.squares[king] != (white ? 'K' : 'k') || p.squares[rook] != (white ? 'R' : 'r'))
            throw std::invalid_argument("castling right without home king/rook");
        p.castling |= right;
    }
    if (ep != "-") {
        p.en_passant = read_square(ep);
        const int dir = p.turn == Color::white ? 8 : -8;
        if (p.en_passant / 8 != (p.turn == Color::white ? 5 : 2) ||
            p.squares[p.en_passant] != '.' ||
            p.squares[p.en_passant - dir] != colored('p', opposite(p.turn)) ||
            p.squares[p.en_passant + dir] != '.' || p.halfmove != 0)
            throw std::invalid_argument("inconsistent FEN en passant state");
    }
    return p;
}

std::uint64_t count_paths(Position& p, int depth) {
    if (depth == 0) return 1;
    const auto moves = legal(p);
    if (depth == 1) return moves.size();
    const Position before = p;
    std::uint64_t count = 0;
    for (Move move : moves) {
        apply(p, move);
        const auto child = count_paths(p, depth - 1);
        p = before;
        if (std::numeric_limits<std::uint64_t>::max() - count < child)
            throw std::overflow_error("perft exceeds uint64_t");
        count += child;
    }
    return count;
}
} // namespace

std::string Move::uci() const {
    if (from < 0 || from >= 64 || to < 0 || to >= 64) throw std::invalid_argument("move square out of range");
    return square_name(from) + square_name(to) + (promotion ? std::string(1, promotion) : "");
}
Move Move::from_uci(std::string_view text) {
    if ((text.size() != 4 && text.size() != 5) ||
        (text.size() == 5 && std::string_view("qrbn").find(text[4]) == std::string_view::npos))
        throw std::invalid_argument("move must be coordinate notation, e.g. e2e4 or a7a8q");
    return {read_square(text.substr(0, 2)), read_square(text.substr(2, 2)), text.size() == 5 ? text[4] : char{0}};
}
std::string_view name(Outcome o) {
    switch (o) {
    case Outcome::ongoing: return "ongoing";
    case Outcome::checkmate: return "checkmate";
    case Outcome::stalemate: return "stalemate";
    case Outcome::known_dead_material: return "known_dead_material";
    case Outcome::fivefold_repetition: return "fivefold_repetition";
    case Outcome::seventy_five_moves: return "seventy_five_moves";
    }
    throw std::logic_error("unknown outcome");
}
Board::Board(std::string_view fen) : position_(parse_fen(fen)) {
    repetition_keys_.push_back(repetition_key(position_));
}
std::string Board::fen() const { return to_fen(position_); }
std::vector<Move> Board::legal_moves() const { auto p = position_; return legal(p); }
bool Board::in_check() const { return checked(position_, position_.turn); }
void Board::play(Move move) {
    const auto moves = legal_moves();
    if (std::find(moves.begin(), moves.end(), move) == moves.end()) throw std::invalid_argument("illegal move");
    if (position_.halfmove == std::numeric_limits<int>::max() || position_.fullmove == std::numeric_limits<int>::max())
        throw std::overflow_error("move counters exhausted");
    history_.push_back(position_);
    apply(position_, move);
    repetition_keys_.push_back(repetition_key(position_));
}
void Board::undo() {
    if (history_.empty()) throw std::out_of_range("no move to undo");
    position_ = history_.back();
    history_.pop_back();
    repetition_keys_.pop_back();
}
int Board::repetition_count() const {
    return static_cast<int>(std::count(repetition_keys_.begin(), repetition_keys_.end(), repetition_keys_.back()));
}
bool Board::has_known_dead_material() const {
    int minors = 0, bishops = 0, bishop_color = -1;
    bool same_color = true;
    for (int square = 0; square < 64; ++square) {
        const char t = type(position_.squares[square]);
        if (t == '.' || t == 'k') continue;
        if (t != 'b' && t != 'n') return false;
        ++minors;
        if (t == 'b') {
            ++bishops;
            const int color = (square % 8 + square / 8) % 2;
            if (bishop_color != -1 && bishop_color != color) same_color = false;
            bishop_color = color;
        }
    }
    return minors <= 1 || (minors == bishops && same_color);
}
Outcome Board::outcome() const {
    if (legal_moves().empty()) return in_check() ? Outcome::checkmate : Outcome::stalemate;
    if (has_known_dead_material()) return Outcome::known_dead_material;
    if (position_.halfmove >= 150) return Outcome::seventy_five_moves;
    if (repetition_count() >= 5) return Outcome::fivefold_repetition;
    return Outcome::ongoing;
}
DrawClaims Board::claims_now() const {
    if (outcome() != Outcome::ongoing) return {};
    return {repetition_count() >= 3, position_.halfmove >= 100};
}
DrawClaims Board::claims_after(Move move) const {
    Board next = *this;
    next.play(move); // Also validates input even when current position is terminal.
    if (outcome() != Outcome::ongoing) return {};
    return next.claims_now();
}
std::uint64_t Board::perft(int depth) const {
    if (depth < 0 || depth > 10) throw std::invalid_argument("perft depth must be 0..10");
    auto p = position_;
    return count_paths(p, depth);
}
std::vector<std::pair<Move, std::uint64_t>> Board::divide(int depth) const {
    if (depth < 1 || depth > 10) throw std::invalid_argument("divide depth must be 1..10");
    auto p = position_;
    std::vector<std::pair<Move, std::uint64_t>> result;
    for (Move move : legal_moves()) {
        apply(p, move);
        result.push_back({move, count_paths(p, depth - 1)});
        p = position_;
    }
    return result;
}
} // namespace regicide
