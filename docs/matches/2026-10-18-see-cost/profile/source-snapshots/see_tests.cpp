#include "regicide/see.hpp"

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <functional>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

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

// Independent from production attack generation and swap/back-propagation.
// The public rules API supplies fully legal moves and maintains occupancy.
int material(char piece) {
    switch (static_cast<char>(std::tolower(static_cast<unsigned char>(piece)))) {
    case 'p': return 100;
    case 'n': return 320;
    case 'b': return 330;
    case 'r': return 500;
    case 'q': return 900;
    case 'k': return 20000;
    default: return 0;
    }
}
bool pawn(char piece) { return piece == 'P' || piece == 'p'; }
int immediate_gain(const Board& board, Move move) {
    const auto& position = board.position();
    int gain = material(position.squares[move.to]);
    if (pawn(position.squares[move.from]) && move.to == position.en_passant &&
        position.squares[move.to] == '.') gain = 100;
    if (move.promotion) gain += material(move.promotion) - 100;
    return gain;
}

// This deliberately models the documented LVA heuristic, rather than an
// exhaustive tactical minimax: select the least-valued legal attacker, break
// ties by source square, and use a queen for subsequent pawn promotions.
// Optional stopping applies even in check; SEE only estimates a local exchange.
int legal_lva_reply(Board& board, int target, int depth = 0) {
    expect(depth < 64, "oracle exceeded the bounded exchange sequence");
    bool found = false;
    Move chosen{};
    int least = std::numeric_limits<int>::max();
    for (Move candidate : board.legal_moves()) {
        if (candidate.to != target || board.position().squares[target] == '.') continue;
        if (candidate.promotion && candidate.promotion != 'q') continue;
        const int value = material(board.position().squares[candidate.from]);
        if (!found || value < least || (value == least && candidate.from < chosen.from)) {
            found = true;
            chosen = candidate;
            least = value;
        }
    }
    if (!found) return 0;
    const int gain = immediate_gain(board, chosen);
    board.play(chosen);
    const int continuation = legal_lva_reply(board, target, depth + 1);
    board.undo();
    return std::max(0, gain - continuation);
}
int oracle(Board& board, Move move) {
    const auto& position = board.position();
    const bool en_passant = pawn(position.squares[move.from]) &&
        position.en_passant == move.to && position.squares[move.to] == '.';
    if (position.squares[move.to] == '.' && !en_passant && !move.promotion) return 0;
    const int gain = immediate_gain(board, move);
    board.play(move);
    const int reply = legal_lva_reply(board, move.to);
    board.undo();
    return gain - reply;
}

void check(const char* fen, const char* uci, int expected) {
    Board board(fen);
    const auto saved = board;
    const auto move = Move::from_uci(uci);
    const auto legal = board.legal_moves();
    expect(std::find(legal.begin(), legal.end(), move) != legal.end(),
           std::string("illegal fixture move ") + uci);
    expect(oracle(board, move) == expected, std::string("hand/oracle mismatch ") + uci);
    expect(static_exchange_evaluation(board, move) == expected,
           std::string("wrong SEE for ") + uci + " in " + fen);
    expect(board == saved, "SEE/oracle changed the complete board or repetition history");
}
void differential(Board& board, int& positions, int& moves, int& negative, int& promotions, int& ep) {
    const auto saved = board;
    ++positions;
    for (Move move : board.legal_moves()) {
        const bool is_ep = pawn(board.position().squares[move.from]) &&
            move.to == board.position().en_passant && board.position().squares[move.to] == '.';
        const int expected = oracle(board, move);
        const int actual = static_exchange_evaluation(board, move);
        expect(actual == expected, "legal-move LVA differential mismatch " + move.uci() +
               " in " + board.fen() + ": expected " + std::to_string(expected) +
               ", got " + std::to_string(actual));
        expect(board == saved, "differential query changed complete board/history");
        ++moves;
        negative += actual < 0;
        promotions += move.promotion != 0;
        ep += is_ep;
    }
}
} // namespace

int main() {
    test("free material, poisoned capture and optional stopping", [] {
        check("7k/8/8/3p4/8/8/8/3Q2K1 w - - 0 1", "d1d5", 100);
        check("3r3k/8/8/3p4/8/8/8/3Q2K1 w - - 0 1", "d1d5", -800);
        check("3r3k/8/8/3p4/8/8/3R4/3R2K1 w - - 0 1", "d2d5", 100);
        check("7k/8/8/8/8/8/8/3Q2K1 w - - 0 1", "d1d5", 0);
    });
    test("updated occupancy reveals both black and white x-ray attackers", [] {
        check("4r2k/4q3/8/8/4p3/3Q4/8/4R2K w - - 0 1", "d3e4", -400);
        check("7k/4q3/8/8/4p3/3Q4/8/4R2K w - - 0 1", "d3e4", 100);
        check("4r2k/4q3/8/8/4p3/5P2/4Q3/4R2K w - - 0 1", "f3e4", 100);
        check("4r2k/4q3/8/8/4p3/5P2/4Q3/7K w - - 0 1", "f3e4", 0);
    });
    test("absolute pins exclude illegal recaptures", [] {
        check("2k5/2n5/8/3p4/2B5/8/8/2R4K w - - 0 1", "c4d5", 100);
        check("2k5/2n5/8/3p4/2B5/8/8/7K w - - 0 1", "c4d5", -230);
        // f4 is the earlier knight square, but its move exposes the king.
        // The legal c7 knight (or a more valuable bishop) must still be tried.
        check("5k2/2n5/8/3p4/2B2n2/8/8/5R1K w - - 0 1", "c4d5", -230);
        check("5k2/8/2b5/3p4/2B2n2/8/8/5R1K w - - 0 1", "c4d5", -230);
    });
    test("kings may recapture only onto unattacked squares", [] {
        check("8/8/4k3/3p4/2B5/8/8/3R3K w - - 0 1", "c4d5", 100);
        check("8/8/4k3/3p4/2B5/8/8/7K w - - 0 1", "c4d5", -230);
        // A pinned knight still attacks d5 for the king-move legality rule.
        check("8/8/4k3/3p4/2B5/3KNr2/8/8 w - - 0 1", "c4d5", 100);
    });
    test("en passant removes the off-target pawn and recomputes pins and rays", [] {
        check("3r3k/8/8/3pP3/8/8/8/3R2K1 w - d6 0 1", "e5d6", 100);
        check("3r3k/8/8/3pP3/8/8/8/6K1 w - d6 0 1", "e5d6", 0);
        check("2k5/2p5/8/3pP3/8/8/8/2R3K1 w - d6 0 1", "e5d6", 100);
        check("3r2k1/8/8/8/3Pp3/8/8/3R3K b - d3 0 1", "e4d3", 100);
        check("6k1/8/8/8/3Pp3/8/8/3R3K b - d3 0 1", "e4d3", 0);
    });
    test("initial promotions and promotion recaptures include material increments", [] {
        check("8/P6k/8/8/8/8/8/7K w - - 0 1", "a7a8q", 800);
        check("8/P6k/8/8/8/8/8/7K w - - 0 1", "a7a8n", 220);
        check("1rr5/P6k/8/8/8/8/8/7K w - - 0 1", "a7b8q", 400);
        check("1rr5/P6k/8/8/8/8/8/7K w - - 0 1", "a7b8n", 400);
        check("1r6/P6k/8/8/8/8/8/7K w - - 0 1", "a7a8q", -100);
        check("7K/8/6k1/8/8/8/p7/1n5R w - - 0 1", "h1b1", -980);
        check("7K/8/6k1/8/8/1R6/p7/1n5R w - - 0 1", "h1b1", -80);
    });
    test("LVA agrees with independent legal-move recursion on varied legal positions", [] {
        int positions = 0, moves = 0, negative = 0, promotions = 0, ep = 0;
        for (const char* fen : {
                 "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
                 "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
                 "1rr5/P6k/8/8/8/8/8/7K w - - 0 1",
                 "7K/8/6k1/8/8/1R6/p7/1n5R w - - 0 1",
                 "5rnk/6pp/7N/3Q4/8/8/8/K7 w - - 0 1"}) {
            Board board(fen);
            differential(board, positions, moves, negative, promotions, ep);
        }
        Board board;
        // Stable non-random seed and an explicit bounded walk, including the
        // actual reversible history rather than rebuilding each position's FEN.
        std::uint32_t seed = 20261018;
        for (int ply = 0; ply < 80; ++ply) {
            if (ply % 2 == 0) differential(board, positions, moves, negative, promotions, ep);
            const auto legal = board.legal_moves();
            if (legal.empty()) break;
            seed = seed * 1664525u + 1013904223u;
            board.play(legal[seed % legal.size()]);
        }
        expect(positions >= 40 && moves >= 500 && negative > 0 && promotions > 0 && ep > 0,
               "bounded differential did not exercise the intended feature coverage");
        std::cout << "SEE differential: " << positions << " positions, " << moves << " moves, "
                  << negative << " losing exchanges, " << promotions << " promotions, "
                  << ep << " en passant\n";
    });
    test("repeated calls preserve castling repetition and undo history", [] {
        Board board;
        for (int repeat = 0; repeat < 2; ++repeat)
            for (const char* move : {"g1f3", "g8f6", "f3g1", "f6g8"}) board.play(move);
        const auto saved = board;
        const auto claims = board.claims_now();
        const auto key = board.position_key(), context = board.repetition_context_key();
        for (int repeat = 0; repeat < 3; ++repeat)
            for (Move move : board.legal_moves()) {
                (void)static_exchange_evaluation(board, move);
                expect(board == saved && board.position_key() == key &&
                           board.repetition_context_key() == context && board.claims_now() == claims,
                       "SEE query altered state used for undo, castling or draw claims");
            }
        board.undo();
        Board expected = saved;
        expected.undo();
        expect(board == expected, "SEE changed subsequent undo behavior");
    });
    test("compact attack lists agree with legal recursion across deterministic walks", [] {
        int positions = 0, moves = 0, negative = 0, promotions = 0, ep = 0;
        // These independent walks exercise edge/corner attack geometry and
        // both colors after many changing occupancies, including full history.
        for (std::uint32_t initial : {1u, 73u, 20261019u, 0xabcdefu}) {
            Board board;
            auto seed = initial;
            for (int ply = 0; ply < 100; ++ply) {
                if (ply % 5 == 0) differential(board, positions, moves, negative, promotions, ep);
                const auto legal = board.legal_moves();
                if (legal.empty()) break;
                seed = seed * 1664525u + 1013904223u;
                board.play(legal[seed % legal.size()]);
            }
        }
        expect(positions >= 60 && moves >= 1500 && negative > 0,
               "deterministic geometry coverage was unexpectedly small");
        std::cout << "Compact SEE differential: " << positions << " positions, " << moves
                  << " moves, " << negative << " losing exchanges\n";
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
