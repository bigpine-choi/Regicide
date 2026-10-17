#include "regicide/board.hpp"

#include <algorithm>
#include <functional>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>

using namespace regicide;
namespace {
int assertions = 0, groups = 0, failures = 0;
void expect(bool condition, const std::string& message) {
    ++assertions;
    if (!condition) throw std::runtime_error(message);
}
template<class F> void rejects(F action, const std::string& message) {
    bool threw = false;
    try { action(); } catch (const std::invalid_argument&) { threw = true; }
    expect(threw, message);
}
void test(const std::string& name, const std::function<void()>& action) {
    ++groups;
    try { action(); std::cout << "PASS " << name << '\n'; }
    catch (const std::exception& e) { ++failures; std::cerr << "FAIL " << name << ": " << e.what() << '\n'; }
}
bool has(const Board& b, std::string_view uci) {
    const auto moves = b.legal_moves();
    return std::find(moves.begin(), moves.end(), Move::from_uci(uci)) != moves.end();
}
void play(Board& b, std::initializer_list<std::string_view> moves) { for (auto move : moves) b.play(move); }
void roundtrip(Board& board, int depth) {
    const auto saved = board;
    for (auto move : board.legal_moves()) {
        board.play(move);
        if (depth > 1) roundtrip(board, depth - 1);
        board.undo();
        expect(board == saved, "entire board/history changed after " + move.uci());
    }
}
struct Fixture { const char* name; const char* fen; std::vector<std::uint64_t> counts; };
const Fixture fixtures[] = {
    {"start", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", {1,20,400,8902,197281,4865609}},
    {"kiwipete", "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1", {1,48,2039,97862,4085603}},
    {"rook-pawn-endgame", "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1", {1,14,191,2812,43238,674624}},
    {"promotion-castling", "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1", {1,6,264,9467,422333}},
    {"promotion-check", "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8", {1,44,1486,62379,2103487}},
    {"middlegame", "r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10", {1,46,2079,89890,3894594}}
};
}
int main() {
    test("exact initial moves and FEN", [] {
        Board b;
        std::set<std::string> actual, expected;
        for (auto m : b.legal_moves()) actual.insert(m.uci());
        for (char f = 'a'; f <= 'h'; ++f) {
            expected.insert(std::string(1,f) + "2" + f + "3");
            expected.insert(std::string(1,f) + "2" + f + "4");
        }
        for (const char* m : {"b1a3","b1c3","g1f3","g1h3"}) expected.insert(m);
        expect(actual == expected, "initial move set");
        expect(b.fen() == Board::start_fen, "start FEN roundtrip");
        expect(!b.in_check(), "initial check");
    });
    for (const auto& f : fixtures) {
        test(std::string("perft ") + f.name, [&] {
            Board b(f.fen);
            const auto saved = b;
            for (std::size_t depth = 0; depth < f.counts.size(); ++depth)
                expect(b.perft(static_cast<int>(depth)) == f.counts[depth], std::string(f.name) + " depth " + std::to_string(depth));
            std::uint64_t total = 0;
            for (auto [move, nodes] : b.divide(3)) { (void)move; total += nodes; }
            expect(total == f.counts[3], "divide sum");
            expect(saved == b, "perft mutated board");
            roundtrip(b, 2);
            std::cout << "  depth " << f.counts.size() - 1 << " nodes " << f.counts.back() << '\n';
        });
    }
    test("castling moves and exact restoration", [] {
        for (const char* side : {"w", "b"}) {
            Board b(std::string("r3k2r/8/8/8/8/8/8/R3K2R ") + side + " KQkq - 0 1");
            expect(has(b, side[0] == 'w' ? "e1g1" : "e8g8"), "kingside castle missing");
            expect(has(b, side[0] == 'w' ? "e1c1" : "e8c8"), "queenside castle missing");
            roundtrip(b, 2);
        }
        Board b("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1");
        b.play("e1g1");
        expect(b.fen() == "r3k2r/8/8/8/8/8/8/R4RK1 b kq - 1 1", "rook relocation/castling rights");
    });
    test("castling through attack and blocked queenside", [] {
        for (const char* fen : {
            "4r1k1/8/8/8/8/8/8/R3K2R w KQ - 0 1",
            "1k3r2/8/8/8/8/8/8/R3K2R w KQ - 0 1",
            "1k4r1/8/8/8/8/8/8/R3K2R w KQ - 0 1"}) {
            Board b(fen);
            expect(!has(b, "e1g1"), "castle out of/through/into check");
        }
        Board blocked("4k3/8/8/8/8/8/8/RN2K2R w KQ - 0 1");
        expect(!has(blocked, "e1c1"), "b1 must be empty");
        Board rook_attacked("r3k3/8/8/8/8/8/8/R3K2R w KQ - 0 1");
        expect(has(rook_attacked, "e1c1"), "attacked rook may castle");
    });
    test("castling rights lost on move or capture", [] {
        Board b("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1");
        play(b, {"h1h2","a8a7","h2h1","a7a8"});
        expect(b.position().castling == 6, "rook returning must not regain rights");
        play(b, {"e1e2","e8e7","e2e1","e7e8"});
        expect(b.position().castling == 0, "king returning must not regain rights");
        Board c("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1");
        c.play("a1a8");
        expect(c.position().castling == 5, "captured home rook retained rights");
    });
    test("en passant both colors, expiration, restoration", [] {
        Board w("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2");
        expect(has(w, "e5d6"), "white en passant");
        roundtrip(w, 2);
        w.play("e5d6");
        expect(w.fen() == "4k3/8/3P4/8/8/8/8/4K3 b - - 0 2", "white en passant state");
        w.undo();
        play(w, {"e1f1","e8f8"});
        expect(!has(w, "e5d6"), "en passant expired");
        Board b("4k3/8/8/8/3Pp3/8/8/4K3 b - d3 0 2");
        b.play("e4d3");
        expect(b.fen() == "4k3/8/8/8/8/3p4/8/4K3 w - - 0 3", "black en passant state");
    });
    test("en passant king safety and check evasion", [] {
        Board horizontal("7k/8/8/r4pPK/8/8/8/8 w - f6 0 1");
        expect(!has(horizontal, "g5f6"), "en passant opened rook line");
        Board vertical("k3r3/8/8/3pP3/8/8/8/4K3 w - d6 0 1");
        expect(!has(vertical, "e5d6"), "pinned pawn en passant");
        Board evasion("7k/8/8/3pP3/4K3/8/8/8 w - d6 0 1");
        expect(evasion.in_check() && has(evasion, "e5d6"), "en passant must remove checking pawn");
    });
    test("all promotions and capture promotions", [] {
        for (const char* fen : {"1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1", "4k3/8/8/8/8/8/p7/1R2K3 b - - 0 1"}) {
            Board b(fen);
            const bool white = b.position().turn == Color::white;
            for (char p : {'q','r','b','n'}) {
                expect(has(b, std::string(white ? "a7a8" : "a2a1") + p), "promotion missing");
                expect(has(b, std::string(white ? "a7b8" : "a2b1") + p), "capture promotion missing");
            }
            expect(!has(b, white ? "a7a8" : "a2a1"), "promotion cannot omit piece");
            roundtrip(b, 2);
        }
    });
    test("pinned piece attacks and double check", [] {
        Board pinned("4k3/4n3/8/8/5K2/8/8/4R3 w - - 0 1");
        expect(!has(pinned, "f4f5"), "pinned knight still attacks f5");
        Board double_check("4r2k/8/8/8/1b6/8/8/4K3 w - - 0 1");
        expect(double_check.in_check(), "double check not detected");
        for (auto m : double_check.legal_moves()) expect(m.from == 4, "non-king move in double check");
    });
    test("checkmate, stalemate and 75-move priority", [] {
        Board mate("7k/6Q1/5K2/8/8/8/8/8 b - - 150 1");
        expect(mate.outcome() == Outcome::checkmate, "mate before 75 moves");
        Board stale("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1");
        expect(stale.outcome() == Outcome::stalemate, "stalemate");
        Board last("7k/5K2/6R1/8/8/8/8/8 w - - 149 1");
        last.play("g6h6");
        expect(last.outcome() == Outcome::checkmate && last.position().halfmove == 150, "mating 150th halfmove");
    });
    test("50-move claim vs automatic 75-move draw", [] {
        Board b("7k/8/8/8/8/8/R7/4K3 w - - 99 1");
        expect(!b.claims_now().fifty_moves, "99 halfmoves");
        const auto saved = b;
        expect(b.claims_after(Move::from_uci("a2a3")).fifty_moves, "intended 100th halfmove");
        expect(b == saved, "claim query mutated state");
        b.play("a2a3");
        expect(b.claims_now().fifty_moves && b.outcome() == Outcome::ongoing, "50 moves not automatic");
        Board auto_draw("7k/8/8/8/8/8/R7/4K3 w - - 150 1");
        expect(auto_draw.outcome() == Outcome::seventy_five_moves, "75 moves automatic");
        Board reset;
        reset.play("e2e4");
        expect(reset.position().halfmove == 0 && reset.position().en_passant == 20, "pawn resets clock and sets raw FEN ep");
        Board capture("7k/8/8/8/8/r7/R7/4K3 w - - 99 1");
        capture.play("a2a3");
        expect(capture.position().halfmove == 0, "capture resets clock");
    });
    test("threefold claims, fivefold draw and history undo", [] {
        Board b;
        for (int cycle = 0; cycle < 4; ++cycle) {
            play(b, {"g1f3","g8f6","f3g1"});
            if (cycle == 1) expect(b.claims_after(Move::from_uci("f6g8")).threefold, "intended third repetition");
            b.play("f6g8");
            expect(b.repetition_count() == cycle + 2, "repetition count");
            if (cycle == 1) expect(b.claims_now().threefold && b.outcome() == Outcome::ongoing, "threefold not automatic");
        }
        expect(b.outcome() == Outcome::fivefold_repetition, "fivefold automatic");
        b.undo();
        expect(b.outcome() == Outcome::ongoing, "undo fivefold history");
        while (b.history_size()) b.undo();
        expect(b == Board(), "multi-ply undo did not restore history");
    });
    test("repetition identity: en passant legality and castling", [] {
        Board unused;
        unused.play("a2a4");
        play(unused, {"g8f6","g1f3","f6g8","f3g1"});
        expect(unused.repetition_count() == 2, "unusable en passant affects identity");
        Board usable("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2");
        play(usable, {"e1f1","e8f8","f1e1","f8e8"});
        expect(usable.repetition_count() == 1, "legal en passant omitted from identity");
        Board pinned("k3r3/8/8/3pP3/8/8/8/4K3 w - d6 0 1");
        play(pinned, {"e1f1","a8b8","f1e1","b8a8"});
        expect(pinned.repetition_count() == 2, "illegal pinned en passant affects identity");
        Board castle("4k3/8/8/8/8/8/8/4K2R w K - 0 1");
        play(castle, {"h1h2","e8e7","h2h1","e7e8"});
        expect(castle.repetition_count() == 1, "castling rights omitted from identity");
        expect(Board(unused.fen()).repetition_count() == 1, "FEN cannot recover previous history");
    });
    test("conservative dead-material classification", [] {
        for (const char* fen : {
            "7k/8/8/8/8/8/8/K7 w - - 0 1",
            "7k/8/8/8/8/8/8/KN6 w - - 0 1",
            "7k/8/8/8/8/8/8/KB6 w - - 0 1",
            "7k/8/8/5b2/8/8/8/KB6 w - - 0 1"})
            expect(Board(fen).outcome() == Outcome::known_dead_material, "known dead material");
        for (const char* fen : {
            "7k/8/8/8/8/8/8/KNN5 w - - 0 1",
            "7k/8/8/8/8/8/8/KNB5 w - - 0 1",
            "7k/8/8/4b3/8/8/8/KB6 w - - 0 1"})
            expect(!Board(fen).has_known_dead_material(), "false dead-material draw");
        expect(Board("7k/8/8/8/8/8/8/K7 w - - 0 1").perft(1) == 3, "perft must ignore draws");
    });
    test("invalid moves and malformed FEN leave state intact", [] {
        Board b;
        const auto saved = b;
        for (const char* m : {"e2e5","e2e4q","e7e5","e1e2","a0a1","0000","e2e4junk","a7a8k"}) {
            rejects([&] { b.play(m); }, std::string("accepted ") + m);
            expect(b == saved, "failed move mutated board");
        }
        rejects([&] { b.play(Move{-1,64}); }, "out of range move");
        bool empty_undo = false;
        try { b.undo(); } catch (const std::out_of_range&) { empty_undo = true; }
        expect(empty_undo && b == saved, "empty undo");
        for (const char* fen : {
            "", "8/8/8/8/8/8/8/8 w - - 0 1", "7k/8/8/8/8/8/8/K7 x - - 0 1",
            "7k/8/8/8/8/8/8/K7 w K - 0 1", "7k/8/8/8/8/8/8/K7 w - a3 0 1",
            "7k/8/8/8/8/8/8/K7 w - - -1 1", "7k/8/8/8/8/8/8/K7 w - - 0 0",
            "7k/8/8/8/8/8/8/K7 w - - 0 1 extra", "7k/8/8/8/8/8/8/K8 w - - 0 1",
            "7k/8/8/8/8/8/8/K6P w - - 0 1", "8/8/8/8/8/8/k7/K7 w - - 0 1",
            "7k/8/8/8/8/8/8/K7 w - - 999999999999999999 1",
            "4k3/8/8/3pP3/8/8/8/4K3 w - d6 1 2",
            "4k3/8/8/8/8/8/8/4K2R w KK - 0 1"})
            rejects([&] { Board invalid(fen); }, std::string("accepted FEN: ") + fen);
        rejects([] { Board invalid("7k/44/8/8/8/8/8/K7 w - - 0 1"); }, "consecutive empty-square digits");
        rejects([&] { (void)b.perft(-1); }, "negative perft depth");
        rejects([&] { (void)b.divide(0); }, "zero divide depth");
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
