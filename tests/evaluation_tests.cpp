#include "regicide/evaluation.hpp"
#include "reference_evaluation_v054.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdlib>
#include <cstdint>
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

int white_score(const Board& board, bool shield = true, bool structure = true) {
    const int score = evaluate_position(board, shield, structure);
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

// This corpus compares against an untouched release implementation. It does
// not reproduce the optimized lookup tables or derive expected scores from
// their internals, so an incorrect table, index, or rounding step is visible.
int oracle_positions = 0;
void compare_release_evaluation(const Board& board) {
    const auto saved = board;
    ++oracle_positions;
    for (bool enabled : {false, true}) {
        expect(evaluate_position(board, enabled, false) == evaluate_position_v054(board, enabled),
               "v0.5.4 oracle mismatch (shield " + std::string(enabled ? "on): " : "off): ") +
                   board.fen());
    }
    expect(board == saved, "release oracle comparison changed position, hashes, or history");
}

// Exercise evaluator inputs rather than simulate the evaluator. The FEN
// reader checks each fixture, including the previously moving king's safety.
// Home-square counts rotate across all 0..4 values; the king occupying one of
// those squares makes four undeveloped minors impossible and naturally caps
// that particular fixture at three.
Board oracle_fixture(int king_square, int pawn_distance, int attacking_phase,
                     int requested_home_minors, int& actual_home_minors) {
    Position position;
    position.squares.fill('.');
    position.squares[king_square] = 'K';
    actual_home_minors = 0;
    for (const auto& [home, piece] : std::array<std::pair<int, char>, 4>{{
             {1, 'N'}, {2, 'B'}, {5, 'B'}, {6, 'N'}}}) {
        if (actual_home_minors == requested_home_minors) break;
        if (position.squares[home] != '.') continue;
        position.squares[home] = piece;
        ++actual_home_minors;
    }
    const int pawn_rank = king_square / 8 + pawn_distance;
    if (pawn_distance && pawn_rank >= 1 && pawn_rank <= 6) {
        for (int file = std::max(0, king_square % 8 - 1);
             file <= std::min(7, king_square % 8 + 1); ++file)
            position.squares[pawn_rank * 8 + file] = 'P';
    }
    // Queens, rooks and knights supply exact material phases, including
    // promoted armies beyond both the shelter and blended-phase clamps.
    int remaining_phase = attacking_phase;
    int next_square = 63;
    while (remaining_phase) {
        while (position.squares[next_square] != '.') --next_square;
        const char piece = remaining_phase >= 4 ? 'q' : remaining_phase >= 2 ? 'r' : 'n';
        position.squares[next_square--] = piece;
        remaining_phase -= piece == 'q' ? 4 : piece == 'r' ? 2 : 1;
    }
    for (int candidate = 63; candidate >= 0; --candidate) {
        if (position.squares[candidate] != '.') continue;
        position.squares[candidate] = 'k';
        try { return Board(fen(position)); }
        catch (const std::invalid_argument&) { position.squares[candidate] = '.'; }
    }
    throw std::runtime_error("could not place a legal opposing king in oracle fixture");
}

// Independent scalar specification. Every pawn is compared with every other
// pawn, without the production rank masks, lookup tables, or file summaries.
// Keep old and new middlegame/endgame terms unrounded until their one blend:
// adding a separately rounded pawn delta to the release score is incorrect.
struct PawnTerms { int middlegame = 0; int endgame = 0; };
PawnTerms scalar_pawn_terms(const Board& board) {
    PawnTerms result;
    const auto& squares = board.position().squares;
    for (int from = 0; from < 64; ++from) {
        const char pawn = squares[from];
        if (pawn != 'P' && pawn != 'p') continue;
        const bool white = pawn == 'P';
        const char enemy = white ? 'p' : 'P';
        const int direction = white ? 1 : -1;
        const int sign = white ? 1 : -1;
        const int file = from % 8;
        const int rank = white ? from / 8 : 7 - from / 8;
        bool adjacent_friend = false, frontmost = true, passed = true;
        bool previous_on_file = false;
        for (int other = 0; other < 64; ++other) {
            if (other == from) continue;
            const int distance = std::abs(other % 8 - file);
            const bool ahead = (other / 8 - from / 8) * direction > 0;
            if (squares[other] == pawn) {
                adjacent_friend |= distance == 1;
                if (!distance) {
                    frontmost &= !ahead;
                    // Charge each extra pawn once, independent of direction.
                    previous_on_file |= other < from;
                }
            } else if (squares[other] == enemy && distance <= 1 && ahead) {
                passed = false;
            }
        }
        int middle = 0, ending = 0;
        if (!adjacent_friend) { middle -= 10; ending -= 12; }
        if (previous_on_file) { middle -= 8; ending -= 12; }
        if (passed && frontmost) {
            // A specification expressed by rank cases rather than the
            // production table: increasing rewards as promotion approaches.
            switch (rank) {
            case 2: middle += 5; ending += 10; break;
            case 3: middle += 10; ending += 20; break;
            case 4: middle += 20; ending += 40; break;
            case 5: middle += 35; ending += 70; break;
            case 6: middle += 60; ending += 120; break;
            default: break;
            }
            const int forward = from + direction * 8;
            if (forward >= 0 && forward < 64 && squares[forward] != '.') {
                // Halve the reward only, never isolation/doubled costs.
                int reward_middle = 0, reward_ending = 0;
                switch (rank) {
                case 2: reward_middle = 5; reward_ending = 10; break;
                case 3: reward_middle = 10; reward_ending = 20; break;
                case 4: reward_middle = 20; reward_ending = 40; break;
                case 5: reward_middle = 35; reward_ending = 70; break;
                case 6: reward_middle = 60; reward_ending = 120; break;
                default: break;
                }
                middle -= reward_middle - reward_middle / 2;
                ending -= reward_ending - reward_ending / 2;
            }
        }
        result.middlegame += sign * middle;
        result.endgame += sign * ending;
    }
    return result;
}

int scalar_position(const Board& board, bool shield, bool structure) {
    const auto& squares = board.position().squares;
    auto piece_type = [](char piece) {
        return static_cast<char>(std::tolower(static_cast<unsigned char>(piece)));
    };
    auto home = [&](bool white) {
        int count = 0;
        const int rank = white ? 0 : 56;
        for (const auto& [file, piece] : std::array<std::pair<int, char>, 4>{{
                 {1, 'n'}, {2, 'b'}, {5, 'b'}, {6, 'n'}}}) {
            const char actual = squares[rank + file];
            count += piece_type(actual) == piece &&
                     static_cast<bool>(std::isupper(static_cast<unsigned char>(actual))) == white;
        }
        return count;
    };
    int material = 0, middle = 0, ending = 0, phase = 0;
    int white_phase = 0, black_phase = 0;
    for (int at = 0; at < 64; ++at) {
        const char piece = squares[at];
        if (piece == '.') continue;
        const bool white = std::isupper(static_cast<unsigned char>(piece)) != 0;
        const int sign = white ? 1 : -1;
        const int rank = white ? at / 8 : 7 - at / 8;
        const int file_center = std::min(at % 8, 7 - at % 8);
        const int center = file_center + std::min(rank, 7 - rank);
        int value = 0, mg = 0, eg = 0, weight = 0;
        switch (piece_type(piece)) {
        case 'p': {
            value = 100;
            const int advanced = std::max(0, rank - 3);
            mg = 4 * rank + 3 * file_center + (rank >= 2 && rank <= 4 ? 3 * file_center : 0);
            eg = 8 * rank + 2 * file_center + 4 * advanced * advanced;
            break;
        }
        case 'n': value = 320; weight = 1; mg = 6 * center + (rank > 0 ? 12 : 0); eg = 4 * center; break;
        case 'b': value = 330; weight = 1; mg = 4 * center + (rank > 0 ? 10 : 0); eg = 4 * center; break;
        case 'r':
            value = 500; weight = 2;
            mg = 2 * file_center + (rank == 6 ? 18 : 0) -
                 (rank > 0 && rank < 5 ? 3 * home(white) : 0);
            eg = 2 * file_center + (rank == 6 ? 20 : 0); break;
        case 'q': value = 900; weight = 4; mg = 2 * center - (rank > 0 ? 4 * home(white) : 0); eg = 3 * center; break;
        case 'k': mg = -7 * center - 5 * rank + (rank == 0 && (at % 8 <= 2 || at % 8 >= 6) ? 25 : 0); eg = 8 * center; break;
        default: break;
        }
        material += sign * value; middle += sign * mg; ending += sign * eg;
        phase += weight;
        (white ? white_phase : black_phase) += weight;
    }
    if (shield) {
        for (int king = 0; king < 64; ++king) {
            const char piece = squares[king];
            if (piece != 'K' && piece != 'k') continue;
            const bool white = piece == 'K';
            const char pawn = white ? 'P' : 'p';
            const int direction = white ? 1 : -1;
            int cost = 0;
            for (int file = std::max(0, king % 8 - 1); file <= std::min(7, king % 8 + 1); ++file) {
                int nearest = 8;
                for (int at = 0; at < 64; ++at) {
                    const int distance = (at / 8 - king / 8) * direction;
                    if (squares[at] == pawn && at % 8 == file && distance > 0)
                        nearest = std::min(nearest, distance);
                }
                cost += nearest <= 3 ? (nearest - 1) * 4 : 12;
            }
            middle -= (white ? 1 : -1) * cost * std::min(white ? black_phase : white_phase, 12) / 12;
        }
    }
    if (structure) {
        const auto pawns = scalar_pawn_terms(board);
        middle += pawns.middlegame;
        ending += pawns.endgame;
    }
    phase = std::min(phase, 24);
    const int score = material + (middle * phase + ending * (24 - phase)) / 24;
    return board.position().turn == Color::white ? score : -score;
}

int structure_positions = 0;
void compare_structure(const Board& board) {
    ++structure_positions;
    const auto saved = board;
    for (bool shield : {false, true}) {
        expect(scalar_position(board, shield, false) == evaluate_position_v054(board, shield),
               "scalar baseline differs from untouched release oracle: " + board.fen());
        for (bool structure : {false, true}) {
            expect(evaluate_position(board, shield, structure) == scalar_position(board, shield, structure),
                   "independent scalar pawn oracle mismatch: " + board.fen());
        }
    }
    expect(evaluate_position(board) == evaluate_position(board, true, true),
           "pawn structure must be enabled by default");
    expect(board == saved, "pawn evaluation changed position, history, hashes or clocks");
}

Board pawn_fixture(std::initializer_list<std::pair<const char*, char>> pieces,
                   int material_phase = 0) {
    Position position;
    position.squares.fill('.');
    position.squares[square("h1")] = 'K';
    position.squares[square("h8")] = 'k';
    for (const auto& [at, piece] : pieces) position.squares[square(at)] = piece;
    int remaining = material_phase;
    for (int at : {56, 57, 58, 59, 60, 61, 62}) {
        if (!remaining) break;
        if (position.squares[at] != '.') continue;
        const char piece = remaining >= 4 ? 'q' : remaining >= 2 ? 'r' : 'n';
        position.squares[at] = piece;
        remaining -= piece == 'q' ? 4 : piece == 'r' ? 2 : 1;
    }
    expect(remaining == 0, "pawn fixture did not supply requested phase");
    return Board(fen(position));
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
            expect(evaluate_position(board, false, false) == expected,
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
    test("frozen v0.5.4 oracle across king squares pawn distances phases and home minors", [] {
        std::array<int, 64> king_coverage{};
        std::array<int, 5> distance_coverage{}, home_coverage{};
        constexpr std::array<int, 17> phases{{
            0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 24, 28}};
        std::array<int, phases.size()> phase_coverage{};
        for (int king = 0; king < 64; ++king) {
            for (int distance = 0; distance <= 4; ++distance) {
                for (std::size_t phase_index = 0; phase_index < phases.size(); ++phase_index) {
                    int actual_home = 0;
                    const auto board = oracle_fixture(king, distance, phases[phase_index],
                        (king + distance + static_cast<int>(phase_index)) % 5, actual_home);
                    ++king_coverage[king];
                    ++distance_coverage[distance];
                    ++phase_coverage[phase_index];
                    ++home_coverage[actual_home];
                    compare_release_evaluation(board);
                    compare_release_evaluation(reflected(board, true));
                    if ((king + distance + static_cast<int>(phase_index)) % 4 == 0 &&
                        !board.in_check()) {
                        Position other_turn = board.position();
                        other_turn.turn = opposite(other_turn.turn);
                        compare_release_evaluation(Board(fen(other_turn)));
                    }
                }
            }
        }
        for (int count : king_coverage) expect(count > 0, "an oracle king square was uncovered");
        for (int count : distance_coverage) expect(count > 0, "an oracle pawn distance was uncovered");
        for (int count : phase_coverage) expect(count > 0, "an oracle attacking phase was uncovered");
        for (int count : home_coverage) expect(count > 0, "an oracle home-minor count was uncovered");
    });
    test("frozen v0.5.4 oracle on deterministic legal game walks with retained history", [] {
        constexpr std::array<const char*, 8> starts{{
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
            "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
            "4k3/P7/8/8/8/8/7p/4K3 w - - 0 1",
            "qqqqkqqq/qqqqqqqq/8/8/8/8/QQQQQQQQ/QQQQKQQQ w - - 0 1",
            "7k/8/2n5/3P4/2K5/4B3/8/8 b - - 17 32",
            "7k/8/8/8/8/5P2/5PPP/6K1 w - - 99 50",
            "r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1"}};
        std::uint32_t random = 0x5a17d3e9U;
        for (int game = 0; game < 100; ++game) {
            Board board(starts[static_cast<std::size_t>(game) % starts.size()]);
            for (int ply = 0; ply < 40; ++ply) {
                compare_release_evaluation(board);
                if (ply % 4 == 0) compare_release_evaluation(reflected(board, true));
                const auto moves = board.legal_moves();
                if (moves.empty()) break;
                random ^= random << 13;
                random ^= random >> 17;
                random ^= random << 5;
                board.play_generated(moves[random % moves.size()]);
            }
        }
    });
    test("frozen v0.5.4 oracle preserves special move and repetition histories", [] {
        auto sequence = [](const char* source, std::initializer_list<const char*> moves) {
            Board board(source);
            compare_release_evaluation(board);
            for (const char* move : moves) {
                board.play(move);
                compare_release_evaluation(board);
            }
            for (std::size_t remaining = moves.size(); remaining; --remaining) {
                board.undo();
                compare_release_evaluation(board);
            }
        };
        sequence("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", {"e1g1", "e8c8"});
        sequence("7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1", {"e5d6", "h8g8"});
        sequence("4k3/P7/8/8/8/8/7p/4K3 w - - 0 1", {"a7a8q", "e8f7", "a8h8", "h2h1n"});
        sequence("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                 {"g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6", "f3g1", "f6g8"});
        for (const Board& board : {
                 shelter_position("g3", {"f4", "g2", "g4", "g6", "h4"}),
                 shelter_position("g3", {"f2", "g2", "h2"}),
                 shelter_position("a1", {"a2", "a4", "b2", "c2"})}) {
            compare_release_evaluation(board);
            compare_release_evaluation(reflected(board, true));
        }
    });
    test("passed pawns gain promotion proximity and blocked rewards are halved for either color", [] {
        for (const auto& [at, expected] : std::array<std::pair<const char*, int>, 6>{{
                 {"d2", -12}, {"d3", -2}, {"d4", 8}, {"d5", 28}, {"d6", 58}, {"d7", 108}}}) {
            const auto board = pawn_fixture({{at, 'P'}});
            expect(white_score(board, false, true) - white_score(board, false, false) == expected,
                   "unopposed pawn has wrong endgame reward or isolation cost");
            const auto black = reflected(board, false);
            expect(white_score(black, false, true) - white_score(black, false, false) == -expected,
                   "black promotion direction differs from white");
            compare_structure(board); compare_structure(black);
        }
        for (char blocker : {'N', 'n', 'P'}) {
            // d5 receives 40 in an ending. A non-pawn blocker halves that
            // reward to 20 while its -12 isolation cost remains untouched.
            // A friendly pawn instead owns the file's sole passed reward.
            const auto board = pawn_fixture({{"d5", 'P'}, {"d6", blocker}});
            compare_structure(board); compare_structure(reflected(board, true));
            if (blocker != 'P') {
                const auto free = pawn_fixture({{"d5", 'P'}, {"a6", blocker}});
                const int blocked_delta = white_score(board, false, true) - white_score(board, false, false);
                const int free_delta = white_score(free, false, true) - white_score(free, false, false);
                expect(blocked_delta < free_delta, "occupied forward square failed to discount a passed pawn");
            }
        }
        const auto unblocked = pawn_fixture({{"d7", 'P'}});
        const auto promotion_blocked = pawn_fixture({{"d7", 'P'}, {"d8", 'n'}});
        expect(white_score(unblocked, false, true) - white_score(unblocked, false, false) >
                   white_score(promotion_blocked, false, true) - white_score(promotion_blocked, false, false),
               "promotion square occupancy failed to reduce the passer reward");
        compare_structure(promotion_blocked);
    });
    test("enemy pawns block passage only ahead on the same or adjacent files including board edges", [] {
        for (const char* enemy : {"c6", "d6", "e6", "c5", "e5", "c4", "e4", "b6", "f6"}) {
            const auto board = pawn_fixture({{"d5", 'P'}, {enemy, 'p'}});
            compare_structure(board); compare_structure(reflected(board, true));
        }
        for (const char* enemy : {"a6", "b6", "h6", "b5", "b4"}) {
            const auto board = pawn_fixture({{"a5", 'P'}, {enemy, 'p'}});
            compare_structure(board); compare_structure(reflected(board, true));
        }
        // Moving the same enemy pawn from e6 to e5 opens the white passer.
        // The black pawn's status also changes, but its lower relative rank
        // earns a smaller reward. Check the exact whole score independently.
        const auto stopped = pawn_fixture({{"d5", 'P'}, {"e6", 'p'}});
        const auto same_rank = pawn_fixture({{"d5", 'P'}, {"e5", 'p'}});
        expect(white_score(same_rank, false, true) - white_score(same_rank, false, false) >
                   white_score(stopped, false, true) - white_score(stopped, false, false),
               "same-rank enemy pawn was incorrectly considered ahead");
    });
    test("doubled and isolated costs combine while only the foremost pawn earns passage", [] {
        for (const auto& [board, expected] : std::array<std::pair<Board, int>, 4>{{
                 {pawn_fixture({{"c3", 'P'}, {"e3", 'P'}}), -4},
                 {pawn_fixture({{"c3", 'P'}, {"d3", 'P'}}), 20},
                 {pawn_fixture({{"d3", 'P'}, {"d5", 'P'}}), 4},
                 {pawn_fixture({{"d2", 'P'}, {"d3", 'P'}, {"d5", 'P'}}), -20}}}) {
            expect(white_score(board, false, true) - white_score(board, false, false) == expected,
                   "isolated or doubled file costs, or frontmost-only passer reward, are wrong");
            compare_structure(board); compare_structure(reflected(board, true));
        }
        // Same material: merely connecting files eliminates isolation. The
        // two configurations have identical passed-rank rewards.
        const auto isolated = pawn_fixture({{"c3", 'P'}, {"e3", 'P'}});
        const auto connected = pawn_fixture({{"c3", 'P'}, {"d3", 'P'}});
        expect((white_score(connected, false, true) - white_score(connected, false, false)) -
                   (white_score(isolated, false, true) - white_score(isolated, false, false)) == 24,
               "connected neighboring files did not remove one isolation cost per pawn");
    });
    test("pawn terms share one phase blend and preserve the independent king shield", [] {
        bool fractional_blend_exercised = false;
        for (int phase : {0, 1, 2, 3, 5, 7, 11, 12, 13, 16, 23, 24, 28}) {
            const auto board = pawn_fixture({{"d3", 'P'}, {"d5", 'P'}, {"e6", 'p'}}, phase);
            compare_structure(board); compare_structure(reflected(board, true));
            const auto terms = scalar_pawn_terms(board);
            fractional_blend_exercised |= (terms.middlegame * std::min(phase, 24) +
                                          terms.endgame * (24 - std::min(phase, 24))) % 24 != 0;
        }
        expect(fractional_blend_exercised, "phase fixtures never tested truncation after a fractional pawn blend");
        const auto intact = shelter_position("g1", {"f2", "g2", "h2"});
        const auto broken = relocated(intact, "g2", "g4");
        for (const Board& board : {intact, broken}) {
            compare_structure(board);
            for (bool structure : {false, true})
                expect(white_score(board, true, structure) - white_score(board, false, structure) ==
                           white_score(board, true, false) - white_score(board, false, false),
                       "full-phase pawn structure changed the independent shield effect");
        }
        expect(shield_delta(broken) < shield_delta(intact), "new pawn terms hid the existing shelter distinction");
    });
    test("independent pawn oracle exhausts legal two-pawn placements and retained game histories", [] {
        int accepted = 0;
        for (int first = 8; first < 56; ++first) for (int second = 8; second < 56; ++second) {
            if (first == second) continue;
            Position position;
            position.squares.fill('.');
            position.squares[square("h1")] = 'K';
            position.squares[square("h8")] = 'k';
            position.squares[first] = 'P';
            position.squares[second] = 'p';
            try {
                const Board board(fen(position));
                ++accepted;
                compare_structure(board);
                if ((first + second) % 7 == 0) symmetric(board);
            } catch (const std::invalid_argument&) {
                // The strict FEN parser rejects the previously moving king
                // left in check; these are not legal evaluation fixtures.
            }
        }
        expect(accepted > 2000, "too few legal opposing-pawn configurations covered");
        for (int first = 8; first < 56; ++first) for (int second = first + 1; second < 56; ++second) {
            Position position;
            position.squares.fill('.');
            position.squares[square("h1")] = 'K';
            position.squares[square("h8")] = 'k';
            position.squares[first] = position.squares[second] = 'P';
            try { compare_structure(Board(fen(position))); }
            catch (const std::invalid_argument&) {}
        }
        constexpr std::array<const char*, 6> starts{{
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
            "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
            "4k3/P7/8/8/8/8/7p/4K3 w - - 0 1",
            "7k/8/2n5/3P4/2K5/4B3/8/8 b - - 17 32",
            "r3k2r/ppp2ppp/8/3pP3/2P5/8/PP3PPP/R3K2R w KQkq d6 0 20"}};
        std::uint32_t random = 0x9284bc17U;
        int walked = 0;
        for (int game = 0; game < 96; ++game) {
            Board board(starts[static_cast<std::size_t>(game) % starts.size()]);
            for (int ply = 0; ply < 48; ++ply) {
                compare_structure(board); ++walked;
                if (ply % 8 == 0) { symmetric(board); compare_structure(reflected(board, true)); }
                const auto moves = board.legal_moves();
                if (moves.empty()) break;
                random ^= random << 13; random ^= random >> 17; random ^= random << 5;
                board.play_generated(moves[random % moves.size()]);
            }
            while (board.history_size()) { board.undo(); compare_structure(board); }
        }
        expect(walked > 3000, "independent pawn oracle needs thousands of legal history-carrying positions");
    });
    test("pawn structure follows deterministic en passant promotions and their complete undo histories", [] {
        auto sequence = [](const char* source, std::initializer_list<const char*> moves) {
            Board board(source);
            const auto original = board;
            compare_structure(board);
            for (const char* move : moves) {
                board.play(move);
                compare_structure(board);
                compare_structure(reflected(board, true));
            }
            for (std::size_t remaining = moves.size(); remaining; --remaining) {
                board.undo();
                compare_structure(board);
            }
            expect(board == original, "special pawn sequence failed to restore original position and history");
        };
        // En passant removes a pawn from a different square from the target;
        // promotion removes a pawn from structure scoring and changes phase.
        // Both colors and capture/quiet promotions are exercised explicitly.
        sequence("7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1", {"e5d6", "h8g8"});
        sequence("4k3/P7/8/8/8/8/7p/4K3 w - - 0 1",
                 {"a7a8q", "e8f7", "a8h8", "h2h1n"});
        sequence("1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1", {"a7b8q", "e8f7"});
    });
    std::cout << oracle_positions << " frozen v0.5.4 oracle positions (each shield on/off)\n";
    std::cout << structure_positions << " independent pawn-structure oracle positions (all option combinations)\n";
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
