#include "regicide/evaluation.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <functional>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>

using namespace regicide;
namespace {
int assertions = 0, groups = 0, failures = 0;
void expect(bool condition, const std::string& message) {
    ++assertions;
    if (!condition) throw std::runtime_error(message);
}
void test(const std::string& name, const std::function<void()>& action) {
    ++groups;
    try { action(); std::cout << "PASS " << name << '\n'; }
    catch (const std::exception& error) {
        ++failures;
        std::cerr << "FAIL " << name << ": " << error.what() << '\n';
    }
}

std::string fen(const Position& position) {
    std::ostringstream out;
    for (int rank = 7; rank >= 0; --rank) {
        int empty = 0;
        for (int file = 0; file < 8; ++file) {
            const char piece = position.squares[rank * 8 + file];
            if (piece == '.') { ++empty; continue; }
            if (empty) out << empty;
            empty = 0;
            out << piece;
        }
        if (empty) out << empty;
        if (rank) out << '/';
    }
    out << (position.turn == Color::white ? " w " : " b ");
    if (!position.castling) out << '-';
    for (const auto& [mask, symbol] : std::array<std::pair<unsigned, char>, 4>{
             {{1, 'K'}, {2, 'Q'}, {4, 'k'}, {8, 'q'}}})
        if (position.castling & mask) out << symbol;
    out << ' ';
    if (position.en_passant < 0) out << '-';
    else out << static_cast<char>('a' + position.en_passant % 8)
             << static_cast<char>('1' + position.en_passant / 8);
    out << ' ' << position.halfmove << ' ' << position.fullmove;
    return out.str();
}

Board reflected(const Board& board, bool swap_turn) {
    Position result = board.position();
    for (int square = 0; square < 64; ++square) {
        const char piece = board.position().squares[square];
        const char swapped = piece == '.' ? '.' :
            static_cast<char>(std::isupper(static_cast<unsigned char>(piece))
                ? std::tolower(static_cast<unsigned char>(piece))
                : std::toupper(static_cast<unsigned char>(piece)));
        result.squares[square ^ 56] = swapped;
    }
    result.castling = ((result.castling & 3) << 2) | ((result.castling & 12) >> 2);
    if (result.en_passant >= 0) result.en_passant ^= 56;
    if (swap_turn) result.turn = opposite(result.turn);
    return Board(fen(result));
}

int white_score(const Board& board) {
    const int score = evaluate_position(board);
    return board.position().turn == Color::white ? score : -score;
}

void symmetric(const Board& board) {
    const auto saved = board;
    const int score = evaluate_position(board);
    expect(evaluate_position(reflected(board, true)) == score,
           "color/rank reflection with swapped turn must preserve relative score");
    // Changing only the active color isolates the sign convention when both
    // kings are safe (the FEN reader rejects a checked side that just moved).
    if (!board.in_check()) {
        Position flipped = board.position();
        flipped.turn = opposite(flipped.turn);
        // En passant is tied to the next mover; it does not affect evaluation.
        flipped.en_passant = -1;
        expect(evaluate_position(Board(fen(flipped))) == -score,
               "changing turn must negate the evaluation exactly");
    }
    expect(board == saved, "evaluation changed the board or history");
}
} // namespace

int main() {
    test("initial symmetry and color/rank reflections", [] {
        expect(evaluate_position(Board{}) == 0, "initial evaluation must be zero");
        for (const char* source : {
                 "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
                 "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
                 "7k/8/2n5/3P4/2K5/4B3/8/8 b - - 17 32",
                 "4k3/8/8/8/8/8/P7/4K3 w - - 0 1",
                 "q3k2q/8/8/8/8/8/8/Q3K2Q b - - 0 1"})
            symmetric(Board(source));
        Board board;
        for (int ply = 0; ply < 120; ++ply) {
            symmetric(board);
            const auto moves = board.legal_moves();
            if (moves.empty() || board.outcome() != Outcome::ongoing) break;
            board.play(moves[static_cast<std::size_t>((ply * 37 + 11) % moves.size())]);
        }
    });
    test("opening development and center beat edge moves and retreat", [] {
        Board central, edge, pawn_center, pawn_edge;
        central.play("g1f3"); edge.play("g1h3");
        pawn_center.play("e2e4"); pawn_edge.play("a2a3");
        expect(white_score(central) > white_score(edge), "Nf3 should beat Nh3");
        expect(white_score(central) > 0, "developing the knight should improve position");
        expect(white_score(pawn_center) > white_score(pawn_edge), "e4 should beat a3");
        expect(white_score(pawn_center) > 0, "central pawn development should improve position");
        central.play("g8f6");
        const int before = white_score(central);
        central.play("f3g1");
        expect(white_score(central) < before, "an unnecessary return home should lose development");
        int best = -100000;
        std::string best_move;
        Board start;
        for (Move move : start.legal_moves()) {
            start.play(move);
            const int score = white_score(start);
            start.undo();
            if (score > best) { best = score; best_move = move.uci(); }
        }
        expect(best_move == "b1c3" || best_move == "g1f3", "static opening preference should be central knight development");
        expect(start == Board{}, "static move comparisons did not restore the board");
    });
    test("bishops develop and queens wait for minor development", [] {
        Board board;
        board.play("e2e4"); board.play("e7e5");
        const int before = white_score(board);
        Board bishop = board, queen = board;
        bishop.play("f1c4"); queen.play("d1h5");
        expect(white_score(bishop) > before, "Bc4 should improve development");
        expect(white_score(bishop) > white_score(queen), "Bc4 should beat premature Qh5 statically");
        expect(white_score(queen) < before, "premature queen exposure should have a small cost");
    });
    test("rook exposure loses time while minors remain home", [] {
        Board board("rnbqkbnr/pppppppp/8/8/P7/8/1PPPPPPP/RNBQKBNR w KQkq - 0 1");
        const int before = white_score(board);
        board.play("a1a3");
        expect(white_score(board) < before, "early Ra3 should lose time compared with developing minors");
    });
    test("king shelter in middlegame and activity in endgame", [] {
        Board sheltered("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQ2KR w kq - 0 1");
        Board exposed("rnbqkbnr/pppppppp/8/8/4K3/8/PPPPPPPP/RNBQ3R w kq - 0 1");
        expect(white_score(sheltered) > white_score(exposed), "middlegame king should prefer sheltered back rank");
        Board edge("7k/8/8/8/8/8/P7/4K3 w - - 0 1");
        Board central("7k/8/8/8/3K4/8/P7/8 w - - 0 1");
        expect(white_score(central) > white_score(edge), "endgame king should prefer activity in the center");
        Board pawn_home("7k/8/8/8/3K4/8/P7/8 w - - 0 1");
        Board pawn_advanced("7k/P7/8/8/3K4/8/8/8 w - - 0 1");
        expect(white_score(pawn_advanced) > white_score(pawn_home), "advanced endgame pawn should gain value");
        symmetric(sheltered); symmetric(exposed); symmetric(central); symmetric(pawn_advanced);
    });
    test("promotion-rich phase and very large material remain well behaved", [] {
        Board board("qqqqkqqq/qqqqqqqq/8/8/8/8/QQQQQQQQ/QQQQKQQQ w - - 0 1");
        expect(evaluate_position(board) == 0, "phase must clamp when promotions exceed initial material");
        symmetric(board);
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
