#include "regicide/board.hpp"

#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>

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
void play(Board& board, std::initializer_list<std::string_view> moves) {
    for (auto move : moves) board.play(move);
}
void verify_tree(Board& board, int depth) {
    const Board saved = board;
    for (auto move : board.legal_moves()) {
        Board validated = saved;
        validated.play(move);
        board.play_generated(move);
        expect(board == validated, "generated/validated state differ after " + move.uci());
        const Board imported(board.fen());
        expect(board.position_key() == imported.position_key(), "incremental key differs from full recomputation");
        if (board.position().halfmove == 0 || board.position().castling != saved.position().castling) {
            expect(board.repetition_context_key() == imported.repetition_context_key(), "irreversible context retained old positions");
            expect(!board.has_repetition_candidate(), "irreversible move retained repetition candidate");
        }
        if (depth > 1) verify_tree(board, depth - 1);
        board.undo();
        expect(board == saved, "generated move undo changed exact board/history/cache");
    }
}
constexpr std::initializer_list<std::string_view> kingside_cycle = {"g1f3", "g8f6", "f3g1", "f6g8"};
constexpr std::initializer_list<std::string_view> queenside_cycle = {"b1c3", "b8c6", "c3b1", "c6b8"};
}
int main() {
    test("position identity includes pieces turn and rights, excludes clocks", [] {
        const Board start;
        const Board clocks("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 87 42");
        expect(start.position_key() == clocks.position_key(), "clocks changed position key");
        expect(start.repetition_context_key() == clocks.repetition_context_key(), "clocks changed repetition context");
        const Board black("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1");
        expect(start.position_key() != black.position_key(), "side to move omitted");
        const Board rights("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1");
        const Board no_rights("r3k2r/8/8/8/8/8/8/R3K2R w - - 0 1");
        expect(rights.position_key() != no_rights.position_key(), "castling rights omitted");
        Board moved;
        moved.play("g1f3");
        expect(moved.position_key() != start.position_key(), "piece placement omitted");
    });
    test("canonical EP uses legal captures including pins and check evasion", [] {
        for (const auto& pair : {
            std::pair{"4k3/8/8/3p4/8/8/8/4K3 w - d6 0 2", "4k3/8/8/3p4/8/8/8/4K3 w - - 0 2"},
            std::pair{"k3r3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "k3r3/8/8/3pP3/8/8/8/4K3 w - - 0 1"},
            std::pair{"7k/8/8/r4pPK/8/8/8/8 w - f6 0 1", "7k/8/8/r4pPK/8/8/8/8 w - - 0 1"}}) {
            const Board ep(pair.first), none(pair.second);
            expect(ep.position_key() == none.position_key(), "unusable EP changed position identity");
            expect(ep.repetition_context_key() == none.repetition_context_key(), "unusable EP changed history identity");
        }
        for (const auto& pair : {
            std::pair{"4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2", "4k3/8/8/3pP3/8/8/8/4K3 w - - 0 2"},
            std::pair{"7k/8/8/3pP3/4K3/8/8/8 w - d6 0 1", "7k/8/8/3pP3/4K3/8/8/8 w - - 0 1"},
            std::pair{"k2r4/8/8/3PpP2/8/8/8/3K4 w - e6 0 1", "k2r4/8/8/3PpP2/8/8/8/3K4 w - - 0 1"},
            std::pair{"4k3/8/8/8/3Pp3/8/8/4K3 b - d3 0 2", "4k3/8/8/8/3Pp3/8/8/4K3 b - - 0 2"}}) {
            expect(Board(pair.first).position_key() != Board(pair.second).position_key(), "legal EP omitted from position identity");
        }
    });
    test("history digest retains counts and all reversible past positions", [] {
        Board kingside, queenside;
        play(kingside, kingside_cycle);
        play(queenside, queenside_cycle);
        expect(kingside.position_key() == queenside.position_key(), "cycles did not transpose");
        expect(kingside.position().halfmove == queenside.position().halfmove, "clock mismatch");
        expect(kingside.repetition_count() == queenside.repetition_count(), "current repetition count mismatch");
        expect(kingside.repetition_context_key() != queenside.repetition_context_key(), "past noncurrent positions omitted");
        const Board imported(kingside.fen());
        expect(kingside.position_key() == imported.position_key(), "import changed position identity");
        expect(kingside.repetition_context_key() != imported.repetition_context_key(), "import invented prior history");
        const auto context_twice = kingside.repetition_context_key();
        play(kingside, kingside_cycle);
        expect(kingside.repetition_count() == 3, "cached third repetition");
        expect(kingside.repetition_context_key() != context_twice, "repeated count parity erased history");
    });
    test("history digest is independent of order when exact position counts agree", [] {
        Board first, second;
        play(first, kingside_cycle);
        play(first, queenside_cycle);
        play(second, queenside_cycle);
        play(second, kingside_cycle);
        expect(first.position() == second.position(), "reordered cycles ended differently");
        expect(first.repetition_context_key() == second.repetition_context_key(), "count multiset changed with visit order");
        expect(!(first == second), "exact history unexpectedly ignored order");
    });
    test("repetition candidate is conservative and restores across undo", [] {
        Board board;
        expect(!board.has_repetition_candidate(), "fresh board has candidate");
        play(board, {"g1f3", "g8f6", "f3g1"});
        expect(!board.has_repetition_candidate(), "candidate before any repeat");
        board.play("f6g8");
        expect(board.has_repetition_candidate() && board.repetition_count() == 2, "second occurrence not cached");
        const Board saved = board;
        board.play("e2e4");
        expect(!board.has_repetition_candidate() && board.repetition_count() == 1, "pawn move did not reset context");
        board.undo();
        expect(board == saved && board.has_repetition_candidate(), "undo did not restore candidate");
        board.undo();
        expect(!board.has_repetition_candidate(), "undo repeated node retained candidate");
    });
    test("castling-right loss resets context without resetting halfmove clock", [] {
        Board board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 25 1");
        const Board saved = board;
        board.play("h1h2");
        const Board imported(board.fen());
        expect(board.position().halfmove == 26, "rook move reset fifty-move clock");
        expect(board.repetition_context_key() == imported.repetition_context_key(), "rights loss retained unreachable old positions");
        board.undo();
        expect(board == saved, "rights-loss undo changed history");
    });
    test("generated moves and incremental hashes roundtrip normal special positions", [] {
        for (const char* fen : {
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
            "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2",
            "4k3/8/8/8/3Pp3/8/8/4K3 b - d3 0 2",
            "1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1",
            "4k3/8/8/8/8/8/p7/1R2K3 b - - 0 1"}) {
            Board board(fen);
            verify_tree(board, 2);
        }
    });
    test("failed external moves leave keys and history unchanged", [] {
        Board board;
        play(board, kingside_cycle);
        const Board saved = board;
        for (const char* move : {"e2e5", "e7e5", "e2e4q"}) {
            bool rejected = false;
            try { board.play(move); } catch (const std::invalid_argument&) { rejected = true; }
            expect(rejected, "illegal external move accepted");
            expect(board == saved, "rejected move changed cache/history");
        }
        while (board.history_size()) board.undo();
        expect(board == Board(), "full undo failed to restore initial caches");
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
