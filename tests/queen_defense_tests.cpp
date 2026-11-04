#include "regicide/search.hpp"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace regicide;
namespace {
int failures = 0, groups = 0;
void expect(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}
void test(const char* name, const std::function<void()>& action) {
    ++groups;
    try { action(); std::cout << "PASS " << name << '\n'; }
    catch (const std::exception& error) {
        ++failures;
        std::cerr << "FAIL " << name << ": " << error.what() << '\n';
    }
}
void valid_pv(const Board& original, const SearchResult& result) {
    Board board = original;
    expect(result.best_move && !result.pv.empty() && result.pv.front() == *result.best_move,
           "missing or mismatched root PV");
    for (Move move : result.pv) {
        const auto legal = board.legal_moves();
        expect(board.outcome() == Outcome::ongoing &&
               std::find(legal.begin(), legal.end(), move) != legal.end(), "illegal PV");
        board.play(move);
    }
}
SearchOptions options(bool enabled = true) {
    SearchOptions result;
    result.positional = false;
    result.quiescence_checks = false;
    result.quiescence_threats = false;
    result.quiescence_queen_defense = enabled;
    result.transposition = false;
    result.pvs = false;
    result.lmr = false;
    result.see_ordering = false;
    result.hash_mb = 1;
    return result;
}
bool claim(const Board& board) {
    const auto current = board.claims_now();
    return current.threefold || current.fifty_moves;
}
// An unpruned material oracle: explicitly grant the known frontier one full
// reply, then captures/promotions and mandatory check evasions. It does not
// implement or call the production queen-attack detector, windows, ordering,
// cache, SEE, or optional quiet checks.
int oracle(Board& board, int full_plies, int ply, std::uint64_t& visited) {
    expect(++visited < 500000 && ply < 24, "oracle fixture exceeded its small exhaustive scope");
    const auto outcome = board.outcome();
    if (outcome == Outcome::checkmate) return -mate_score + ply;
    if (outcome != Outcome::ongoing) return 0;
    const bool checked = board.in_check();
    int best = full_plies || checked ? -32000 : evaluate_material(board);
    if (claim(board)) best = std::max(best, 0);
    const Position position = board.position();
    for (Move move : board.legal_moves()) {
        const char piece = position.squares[move.from];
        const bool ep = (piece == 'P' || piece == 'p') && move.to == position.en_passant;
        if (!full_plies && !checked && position.squares[move.to] == '.' && !ep && !move.promotion) {
            // An intended quiet claim is available even outside tactical moves.
            if (board.claims_after(move).threefold || board.claims_after(move).fifty_moves)
                best = std::max(best, 0);
            continue;
        }
        board.play(move);
        const bool intended = claim(board);
        int score = -oracle(board, std::max(0, full_plies - 1), ply + 1, visited);
        board.undo();
        if (intended) score = std::max(score, 0);
        best = std::max(best, score);
    }
    return best;
}
Board trapped_root() {
    // After ...Nf6, every one of White's 19 legal replies permits an immediate
    // legal queen capture. This is an 11-piece reduction of the King's leaf,
    // not a claim that queen capture alone proves absent compensation.
    return Board("2b1k1nr/5p2/7p/6pQ/8/5N2/7P/6K1 b - - 0 1");
}
Board mirrored(const Board& original) {
    const auto& position = original.position();
    std::string fen;
    for (int rank = 7; rank >= 0; --rank) {
        int empty = 0;
        for (int file = 0; file < 8; ++file) {
            const char piece = position.squares[(7 - rank) * 8 + file];
            if (piece == '.') { ++empty; continue; }
            if (empty) { fen += static_cast<char>('0' + empty); empty = 0; }
            fen += static_cast<char>(std::islower(static_cast<unsigned char>(piece)) ?
                std::toupper(static_cast<unsigned char>(piece)) : std::tolower(static_cast<unsigned char>(piece)));
        }
        if (empty) fen += static_cast<char>('0' + empty);
        if (rank) fen += '/';
    }
    // The declared synthetic fixtures have no castling or EP rights/history.
    expect(!position.castling && position.en_passant < 0 && !original.history_size(), "unsupported mirror context");
    fen += position.turn == Color::white ? " b - - " : " w - - ";
    fen += std::to_string(position.halfmove) + " " + std::to_string(position.fullmove);
    return Board(fen);
}
void same_graph(const SearchResult& left, const SearchResult& right) {
    expect(left.score == right.score && left.depth == right.depth && left.best_move == right.best_move &&
           left.pv == right.pv && left.nodes == right.nodes && left.qnodes == right.qnodes &&
           left.seldepth == right.seldepth && left.stopped == right.stopped &&
           left.queen_defense_qnodes == right.queen_defense_qnodes,
           "identical search semantics changed result or visited graph");
}
} // namespace

int main() {
    test("trapped queen frontier equals an independent full-reply material oracle", [] {
        const Board root = trapped_root(), saved = root;
        Board frontier = root;
        frontier.play("g8f6");
        expect(!frontier.in_check(), "fixture unexpectedly checks the king");
        expect(frontier.legal_moves().size() == 19, "small frontier fixture changed");
        for (Move reply : frontier.legal_moves()) {
            frontier.play(reply);
            const auto replies = frontier.legal_moves();
            expect(std::any_of(replies.begin(), replies.end(), [&](Move capture) {
                       return frontier.position().squares[capture.to] == 'Q';
                   }), "a legal response no longer permits an immediate queen capture");
            frontier.undo();
        }
        std::uint64_t visited = 0;
        const int expected = -oracle(frontier, 1, 1, visited);
        auto enabled = options();
        enabled.root_moves = std::vector<Move>{Move::from_uci("g8f6")};
        auto disabled = enabled;
        disabled.quiescence_queen_defense = false;
        const auto old = search(root, {.depth = 1}, nullptr, {}, disabled);
        for (bool table : {false, true}) for (bool pvs : {false, true}) for (bool cache : {false, true}) {
            enabled.transposition = table;
            enabled.pvs = pvs;
            enabled.quiescence_cache = cache;
            const auto result = search(root, {.depth = 1}, nullptr, {}, enabled);
            expect(result.depth == 1 && !result.stopped && result.score == expected,
                   "queen defense disagrees with the independent frontier oracle");
            expect(result.queen_defense_qnodes > 0 && result.score > old.score + 300,
                   "fixture did not expose the optimistic stand-pat horizon");
            valid_pv(root, result);
        }
        expect(root == saved, "frontier search changed the input position/history");
    });
    test("rank-and-color mirror retains the same material value", [] {
        const Board root = trapped_root(), mirror = mirrored(root);
        auto first = options(), second = options();
        first.root_moves = std::vector<Move>{Move::from_uci("g8f6")};
        second.root_moves = std::vector<Move>{Move::from_uci("g1f3")};
        const auto a = search(root, {.depth = 1}, nullptr, {}, first);
        const auto b = search(mirror, {.depth = 1}, nullptr, {}, second);
        expect(a.score == b.score && a.queen_defense_qnodes && b.queen_defense_qnodes,
               "queen defense is asymmetric under color/rank reversal");
        valid_pv(mirror, b);
    });
    test("quiet mating counterattack is retained while the queen is en prise", [] {
        const Board board("7k/6pp/8/8/1n6/8/P7/Q3R1K1 b - - 0 1");
        auto selected = options();
        selected.root_moves = std::vector<Move>{Move::from_uci("b4c2")};
        const auto result = search(board, {.depth = 1}, nullptr, {}, selected);
        expect(result.queen_defense_qnodes && result.score == -mate_score + 2 && result.pv.size() == 2 &&
               result.pv[1] == Move::from_uci("e1e8"), "defense filtering lost the nonqueen mating counterattack");
        valid_pv(board, result);
    });
    test("a sound queen offer is preserved before the move exposes its queen", [] {
        Board board;
        for (const char* move : {"e2e4", "e7e5", "g1f3", "d7d6", "f1c4", "c8g4", "b1c3"}) board.play(move);
        const Board saved = board;
        auto selected = options();
        selected.root_moves = std::vector<Move>{Move::from_uci("g7g6")};
        bool saw_offer = false;
        selected.static_evaluation_observer = [&](const Board&, const std::vector<Move>& path,
                                                  const StaticEvaluationObservation&) {
            if (path.size() == 2 && path[1] == Move::from_uci("f3e5")) saw_offer = true;
        };
        const auto result = search(board, {.depth = 1, .nodes = 100000}, nullptr, {}, selected);
        expect(result.depth == 1 && !result.stopped && !result.queen_defense_qnodes && saw_offer,
               "unchanged frontier omitted the independently audited Nxe5 queen offer");
        selected.quiescence_queen_defense = false;
        selected.static_evaluation_observer = {};
        const auto previous = search(board, {.depth = 1, .nodes = 100000}, nullptr, {}, selected);
        same_graph(result, previous);
        expect(board == saved, "queen offer search damaged legal game history");
        valid_pv(board, result);
    });
    test("pinned attacker triggers extra replies without a fabricated material penalty", [] {
        const Board board("6k1/7p/8/6b1/8/8/3Q4/6RK b - - 0 1");
        Board frontier = board;
        frontier.play("h7h6");
        std::uint64_t visited = 0;
        const int expected = -oracle(frontier, 1, 1, visited);
        auto selected = options();
        selected.root_moves = std::vector<Move>{Move::from_uci("h7h6")};
        const auto result = search(board, {.depth = 1}, nullptr, {}, selected);
        expect(result.queen_defense_qnodes && result.score == expected,
               "pinned geometric attacker became an artificial score penalty");
        valid_pv(board, result);
    });
    test("queen exchange checked frontier and no-queen cases preserve the old graph", [] {
        for (const auto& [fen, move] : std::vector<std::pair<const char*, const char*>>{
                {"3q2k1/7p/8/8/8/8/3Q4/7K b - - 0 1", "h7h6"},
                {"1r4k1/8/8/8/8/8/8/Q5K1 b - - 0 1", "b8b1"},
                {"7k/8/8/8/8/8/P7/6K1 w - - 0 1", "a2a3"}}) {
            const Board board(fen);
            auto on = options(), off = options(false);
            on.root_moves = off.root_moves = std::vector<Move>{Move::from_uci(move)};
            const auto a = search(board, {.depth = 1}, nullptr, {}, on);
            const auto b = search(board, {.depth = 1}, nullptr, {}, off);
            expect(!a.queen_defense_qnodes, "irrelevant or already-forced frontier was expanded again");
            same_graph(a, b);
        }
    });
    test("quiet intended fifty-move claim survives the queen emergency", [] {
        const Board board("2b1k1nr/5p2/7p/6pQ/8/5N2/7P/6K1 b - - 98 1");
        auto selected = options();
        selected.root_moves = std::vector<Move>{Move::from_uci("g8f6")};
        const auto result = search(board, {.depth = 1}, nullptr, {}, selected);
        expect(result.score == 0 && result.queen_defense_qnodes, "full-reply horizon removed an intended draw claim");
        valid_pv(board, result);
    });
    test("NNUE incremental and full refresh agree through queen-defense replies", [] {
        const Board board = trapped_root(), saved = board;
        auto incremental = options();
        incremental.nnue = true;
        incremental.transposition = true;
        incremental.quiescence_cache = true;
        incremental.root_moves = std::vector<Move>{Move::from_uci("g8f6")};
        auto full = incremental;
        full.nnue_incremental = false;
        const auto a = search(board, {.depth = 2, .nodes = 10000}, nullptr, {}, incremental);
        const auto b = search(board, {.depth = 2, .nodes = 10000}, nullptr, {}, full);
        same_graph(a, b);
        expect(a.queen_defense_qnodes && board == saved, "NNUE coverage or board restoration missing");
        valid_pv(board, a);
    });
    test("node cancellation and reused storage cannot publish stale queen-defense work", [] {
        Board board = trapped_root();
        for (const char* move : {"g8f6", "g1f1", "f6g8", "f1g1"}) board.play(move);
        const Board saved = board;
        auto on = options();
        on.transposition = true;
        on.quiescence_cache = true;
        on.root_moves = std::vector<Move>{Move::from_uci("g8f6")};
        SearchContext context;
        std::atomic_bool stop{true};
        const auto stopped = search(board, {.depth = 3}, &stop, {}, on, &context);
        expect(stopped.stopped && !stopped.nodes && !stopped.queen_defense_qnodes,
               "preexisting cancellation performed queen-defense work");
        for (std::uint64_t limit : {1U, 50U, 500U}) {
            const auto result = search(board, {.depth = 3, .nodes = limit}, nullptr, {}, on, &context);
            expect(result.nodes <= limit && board == saved, "queen defense exceeded node cap or changed history");
            valid_pv(board, result);
        }
        auto off = on;
        off.quiescence_queen_defense = false;
        const auto fresh = search(board, {.depth = 2, .nodes = 10000}, nullptr, {}, off);
        const auto reused = search(board, {.depth = 2, .nodes = 10000}, nullptr, {}, off, &context);
        same_graph(fresh, reused);
        expect(!reused.queen_defense_qnodes && board == saved, "old option state survived a new search");
    });
    std::cout << groups - failures << '/' << groups << " queen-defense groups passed\n";
    return failures ? 1 : 0;
}
