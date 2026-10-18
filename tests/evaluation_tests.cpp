#include "regicide/evaluation.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <functional>
#include <initializer_list>
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

int white_score(const Board& board, bool shield = true) {
    const int score = evaluate_position(board, shield);
    return board.position().turn == Color::white ? score : -score;
}

// Difference out every old piece-square/material feature. Moving a pawn in
// these fixtures must affect the new shelter term, not just its old advance bonus.
int shield_delta(const Board& board) {
    return white_score(board) - white_score(board, false);
}

int square(const char* coordinate) {
    return (coordinate[1] - '1') * 8 + coordinate[0] - 'a';
}

Board shelter_position(const char* king, std::initializer_list<const char*> pawns) {
    Position position = Board("rnbqkbnr/pppppppp/8/8/8/8/8/6K1 w kq - 0 1").position();
    position.squares[square("g1")] = '.';
    position.squares[square(king)] = 'K';
    for (const char* pawn : pawns) position.squares[square(pawn)] = 'P';
    return Board(fen(position));
}

Board relocated(const Board& board, const char* from, const char* to) {
    Position position = board.position();
    expect(position.squares[square(from)] != '.' && position.squares[square(to)] == '.',
           "relocation fixture requires an occupied source and empty destination");
    position.squares[square(to)] = position.squares[square(from)];
    position.squares[square(from)] = '.';
    return Board(fen(position));
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
    test("disabled shelter preserves pinned pre-change evaluation values", [] {
        // These values belong to the pre-shelter b3a7822 evaluation. Preserve
        // distinct opening, material, king-placement, endgame and promoted cases.
        for (const auto& [source, expected] : std::array<std::pair<const char*, int>, 6>{{
                 {"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 0},
                 {"rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1", -17},
                 {"7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1", 284},
                 {"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQ2KR w kq - 0 1", -630},
                 {"7k/P7/8/8/3K4/8/8/8 w - - 0 1", 232},
                 {"qqqqkqqq/qqqqqqqq/8/8/8/8/QQQQQQQQ/QQQQKQQQ w - - 0 1", 0}}}) {
            Board board(source);
            const auto saved = board;
            expect(evaluate_position(board, false) == expected,
                   "disabling shelter changed a frozen legacy evaluation");
            expect(board == saved, "disabled evaluation changed position or history");
        }
    });
    test("nearby pawns shelter both wings and both colors without hiding their advance cost", [] {
        for (const auto& [king, middle] : std::array<std::pair<const char*, const char*>, 2>{{
                 {"g1", "g2"}, {"c1", "c2"}}}) {
            const Board intact = king[0] == 'g'
                ? shelter_position(king, {"f2", "g2", "h2"})
                : shelter_position(king, {"b2", "c2", "d2"});
            const std::string file(1, middle[0]);
            const auto one_step = relocated(intact, middle, (file + "3").c_str());
            const auto two_steps = relocated(intact, middle, (file + "4").c_str());
            const auto far = relocated(intact, middle, (file + "5").c_str());
            const auto farther = relocated(intact, middle, (file + "6").c_str());
            const auto missing = relocated(intact, middle, "a2");
            expect(shield_delta(intact) == 0, "an intact adjacent shield should have no new cost");
            expect(shield_delta(intact) > shield_delta(one_step) &&
                       shield_delta(one_step) > shield_delta(two_steps) &&
                       shield_delta(two_steps) > shield_delta(far),
                   "moving the same pawn farther away must progressively weaken its shield");
            expect(shield_delta(far) == shield_delta(farther) &&
                       shield_delta(far) == shield_delta(missing),
                   "a remote or missing shield pawn should reach the same bounded cost");
            expect(-shield_delta(one_step) < -shield_delta(missing),
                   "a normal one-step fianchetto must cost less than a missing pawn");
            for (const Board& board : {intact, one_step, two_steps, far, missing}) {
                const auto black = reflected(board, false);
                expect(shield_delta(black) == -shield_delta(board),
                       "black's forward direction or kingside/queenside reflection is wrong");
                symmetric(board);
            }
        }
    });
    test("edge shelter uses two files and ignores a third outside the king neighborhood", [] {
        const auto intact = shelter_position("a1", {"a2", "b2", "c2"});
        const auto outside = relocated(intact, "c2", "c4");
        const auto gap = relocated(intact, "b2", "h2");
        expect(shield_delta(intact) == 0 && shield_delta(outside) == 0,
               "an edge king should require only its own and its one adjacent file");
        expect(shield_delta(gap) < shield_delta(intact),
               "the on-board neighbor at the edge must still supply shelter");
        symmetric(gap);
    });
    test("a pawn behind the king cannot shelter it and the closest forward pawn wins", [] {
        const auto advanced = shelter_position("g3", {"f4", "g4", "h4"});
        const auto behind = relocated(advanced, "g4", "g2");
        const auto absent = relocated(advanced, "g4", "a2");
        expect(shield_delta(advanced) == 0 && shield_delta(behind) < 0,
               "shelter must follow an advanced king and exclude pawns behind it");
        expect(shield_delta(behind) == shield_delta(absent),
               "a pawn behind the king was incorrectly accepted as its shield");
        const auto doubled = shelter_position("g1", {"f2", "g2", "g4", "h2"});
        const auto remote = relocated(doubled, "g4", "g5");
        const auto nearest_missing = relocated(doubled, "g2", "a2");
        expect(shield_delta(doubled) == 0 && shield_delta(remote) == 0,
               "a farther doubled pawn must not overwrite the nearest forward pawn");
        expect(shield_delta(nearest_missing) < 0,
               "the remaining distant pawn must shelter less after the nearest one is removed");
        symmetric(advanced); symmetric(behind); symmetric(doubled);
    });
    test("shelter fades in pawn endings and uses the opponent's attacking material", [] {
        const Board ending("7k/6pp/8/8/8/8/5PPP/6K1 w - - 0 1");
        expect(shield_delta(ending) == 0 && shield_delta(relocated(ending, "g2", "a2")) == 0,
               "a pawn-only ending must not penalize an active king's absent shield");
        // White still has a queen, but Black has only pawns and a complete
        // edge shelter. White's own queen must not manufacture danger to itself.
        const Board no_attacker("7k/6pp/8/8/8/8/5PPP/5QK1 w - - 0 1");
        const auto gap = relocated(no_attacker, "g2", "a2");
        expect(shield_delta(no_attacker) == 0 && shield_delta(gap) == 0,
               "shelter scaling used the owner's material instead of the opponent's");
        symmetric(ending); symmetric(gap);
    });
    test("promoted attacking armies clamp shelter rather than magnifying it without bound", [] {
        const Board promoted("qqqqkqqq/pppppppp/8/8/8/8/5PPP/6K1 w - - 0 1");
        const auto gap = relocated(promoted, "g2", "a2");
        const auto all_gaps = relocated(relocated(gap, "f2", "b2"), "h2", "c2");
        expect(shield_delta(promoted) == 0 && shield_delta(gap) < 0,
               "promotion-rich opponent must still distinguish intact and broken shelter");
        expect(shield_delta(gap) >= -12 && shield_delta(all_gaps) >= -36,
               "extra promoted pieces exceeded the bounded one-file or three-file shelter cost");
        symmetric(promoted); symmetric(all_gaps);
    });
    test("shelter evaluation preserves repetition history hashes and turn antisymmetry", [] {
        Board board;
        for (int repetition = 0; repetition < 2; ++repetition)
            for (const char* move : {"g1f3", "g8f6", "f3g1", "f6g8"}) board.play(move);
        const auto saved = board;
        expect(board.history_size() > 0 && board.claims_now().threefold,
               "history-preservation fixture must carry a real repetition claim");
        for (bool enabled : {false, true}) {
            const auto reflected_board = reflected(board, true);
            expect(evaluate_position(board, enabled) == evaluate_position(reflected_board, enabled),
                   "evaluation mode changed the color/turn convention");
            expect(board == saved, "evaluation mode changed board, clocks, hashes, or history");
        }
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
