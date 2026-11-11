#include "regicide/mate.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <exception>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace regicide;
namespace {
void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}
MateLimits limits(int moves = 1) {
    MateLimits value;
    value.moves = moves;
    value.nodes = 1000000;
    return value;
}
MateLimits only(int moves, const std::string& move) {
    auto value = limits(moves);
    value.root_moves = std::vector<Move>{Move::from_uci(move)};
    return value;
}
MateResult checked(const Board& board, const MateLimits& budget,
                   const std::atomic_bool* stop = nullptr) {
    const Board saved = board;
    auto result = solve_mate(board, budget, stop);
    require(board == saved, "solver changed caller board/history");
    if (budget.nodes) require(result.nodes <= budget.nodes, "node budget exceeded");
    if (result.status == MateStatus::proven) {
        require(!result.pv.empty(), "proof has no PV");
        require(result.mate_plies == static_cast<int>(result.pv.size()), "PV distance disagrees");
        require(result.mate_plies <= 2 * budget.moves - 1, "proof exceeds bound");
        require(result.best_move && *result.best_move == result.pv.front(), "wrong proof bestmove");
        Board replay = board;
        for (const auto move : result.pv) {
            require(replay.outcome() == Outcome::ongoing, "PV continues past terminal");
            if (replay.position().turn != board.position().turn) {
                const auto now = replay.claims_now();
                const auto after = replay.claims_after(move);
                require(!now.threefold && !now.fifty_moves && !after.threefold && !after.fifty_moves,
                        "PV permits defender draw claim");
            }
            replay.play(move);
        }
        require(replay.outcome() == Outcome::checkmate, "PV does not end in mate");
        require(replay.position().turn != board.position().turn, "proof mates attacker");
    } else {
        require(result.pv.empty(), "unproven result exposes PV");
        require(result.mate_plies == 0, "unproven result claims distance");
    }
    if (result.best_move) {
        const auto legal = board.legal_moves();
        require(std::find(legal.begin(), legal.end(), *result.best_move) != legal.end(),
                "fallback is illegal");
    }
    return result;
}
void proven(const Board& board, const MateLimits& budget, int plies) {
    const auto result = checked(board, budget);
    require(result.status == MateStatus::proven, "expected proof: " + result.reason);
    require(result.mate_plies == plies, "wrong mate distance");
}
void refuted(const Board& board, const MateLimits& budget) {
    const auto result = checked(board, budget);
    require(result.status == MateStatus::no_mate_within_bound,
            "expected bounded refutation: " + result.reason);
}
void invalid(const std::function<void()>& operation) {
    bool rejected = false;
    try { operation(); } catch (const std::invalid_argument&) { rejected = true; }
    require(rejected, "invalid request accepted");
}
const std::string m1 = "7k/5K2/6Q1/8/8/8/8/8 w - - 0 1";
const std::string m2 = "7k/8/5K2/8/8/8/Q7/8 w - - 0 1";
const std::string branch2 = "8/7k/5K2/8/8/8/Q7/8 w - - 0 1";
const std::string branch3 = "8/7k/8/5K2/8/8/Q7/8 w - - 0 1";
}
int main() {
    int passed = 0, failed = 0;
    const auto test = [&](const std::string& label, const std::function<void()>& body) {
        try { body(); ++passed; std::cout << "PASS " << label << '\n'; }
        catch (const std::exception& error) {
            ++failed; std::cerr << "FAIL " << label << ": " << error.what() << '\n';
        }
    };
    test("M1 and shortest distance under larger request", [] {
        proven(Board(m1), limits(), 1);
        proven(Board(m1), limits(3), 1);
    });
    test("M2 absent at shorter bound", [] {
        refuted(Board(m2), limits());
        proven(Board(m2), limits(2), 3);
    });
    test("quiet key and both legal defenses", [] {
        const Board board(branch2);
        const auto result = checked(board, only(2, "a2g2"));
        require(result.status == MateStatus::proven && result.mate_plies == 3, "quiet key missing");
        require(!board.gives_check_generated(Move::from_uci("a2g2")), "key is not quiet");
        Board defense = board;
        defense.play("a2g2");
        const auto replies = defense.legal_moves();
        require(replies.size() == 2, "expected two defenses");
        for (const auto reply : replies) {
            Board child = defense;
            child.play(reply);
            proven(child, limits(), 1);
        }
    });
    test("one cooperative mating line is not a proof", [] {
        Board witness(branch3);
        for (const auto* move : {"a2g2", "h7h6", "g2g6"}) witness.play(move);
        require(witness.outcome() == Outcome::checkmate, "cooperative line not mate");
        refuted(Board(branch3), only(2, "a2g2"));
    });
    test("M3 longest defense PV", [] {
        const auto result = checked(Board(branch3), only(3, "a2g2"));
        require(result.status == MateStatus::proven && result.mate_plies == 5, "expected M3");
        require(result.pv[1] == Move::from_uci("h7h8"), "PV did not choose longest defense");
    });
    test("restricted root cannot borrow another proof", [] {
        refuted(Board(m1), only(1, "g6g5"));
        proven(Board(m1), only(1, "g6g7"), 1);
    });
    test("stalemate", [] {
        const Board board("7k/5K2/6Q1/8/8/8/8/8 b - - 0 1");
        require(board.outcome() == Outcome::stalemate, "bad fixture");
        refuted(board, limits(3));
        require(!checked(board, limits()).best_move, "terminal root fallback");
    });
    test("attacker already checkmated", [] {
        const Board board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1");
        require(board.outcome() == Outcome::checkmate, "bad fixture");
        refuted(board, limits(3));
    });
    test("known dead material", [] {
        refuted(Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1"), limits(3));
    });
    test("attacker can decline fifty-move claim", [] {
        const Board board("7k/5K2/6Q1/8/8/8/8/8 w - - 100 1");
        require(board.claims_now().fifty_moves, "bad fixture");
        proven(board, limits(), 1);
    });
    test("defender current fifty-move claim", [] {
        refuted(Board("7k/8/5K2/8/8/8/Q7/8 w - - 99 1"), only(2, "a2g2"));
    });
    test("defender intended fifty-move claim", [] {
        const Board board("7k/8/5K2/8/8/8/Q7/8 w - - 98 1");
        Board defense = board;
        defense.play("a2g2");
        require(!defense.claims_now().fifty_moves, "claim should require intended move");
        require(defense.claims_after(Move::from_uci("h8h7")).fifty_moves, "bad fixture");
        refuted(board, only(2, "a2g2"));
    });
    test("mate at hundredth halfmove outranks optional draw", [] {
        proven(Board("7k/8/5K2/8/8/8/Q7/8 w - - 97 1"), only(2, "a2g2"), 3);
    });
    test("automatic seventy-five-move draw", [] {
        const Board board("7k/5K2/6Q1/8/8/8/8/8 w - - 150 1");
        refuted(board, limits());
        require(!checked(board, limits()).best_move, "automatic draw fallback");
    });
    test("mate at seventy-five-move boundary", [] {
        proven(Board("7k/5K2/6Q1/8/8/8/8/8 w - - 149 1"), limits(), 1);
    });
    test("attacker can leave claimable repeated position", [] {
        Board board(m2);
        for (int cycle = 0; cycle < 2; ++cycle)
            for (const auto* move : {"a2a3", "h8h7", "a3a2", "h7h8"}) board.play(move);
        require(board.claims_now().threefold, "bad fixture");
        proven(board, only(2, "a2g2"), 3);
        refuted(board, only(2, "a2a3"));
    });
    test("defender intended repetition claim", [] {
        Board board(m2);
        for (const auto* move : {"a2a3", "h8h7", "a3a2", "h7h8", "a2a3", "h8h7"}) board.play(move);
        Board defense = board;
        defense.play("a3a2");
        require(!defense.claims_now().threefold, "claim should require intended repetition");
        require(defense.claims_after(Move::from_uci("h7h8")).threefold, "bad fixture");
        refuted(board, only(2, "a3a2"));
    });
    test("automatic fivefold repetition", [] {
        Board board(m2);
        for (int cycle = 0; cycle < 4; ++cycle)
            for (const auto* move : {"a2a3", "h8h7", "a3a2", "h7h8"}) board.play(move);
        require(board.outcome() == Outcome::fivefold_repetition, "bad fixture");
        refuted(board, limits(2));
    });
    test("all four promotion choices including underpromotion", [] {
        const Board board("7k/5P2/6K1/8/8/8/8/8 w - - 0 1");
        for (const auto* move : {"f7f8q", "f7f8r"}) proven(board, only(1, move), 1);
        for (const auto* move : {"f7f8b", "f7f8n"}) refuted(board, only(1, move));
    });
    test("legal en passant discovered mate", [] {
        proven(Board("1N1rkr2/5p2/8/3pP3/8/8/8/K3R3 w - d6 0 1"), only(1, "e5d6"), 1);
    });
    test("castling mate", [] {
        proven(Board("4rkr1/4p1p1/8/8/8/8/8/4K2R w K - 0 1"), only(1, "e1g1"), 1);
    });
    test("strict node cap yields unknown and legal fallback", [] {
        auto budget = limits(16);
        budget.nodes = 1;
        const auto result = checked(Board(), budget);
        require(result.status == MateStatus::unknown && result.best_move.has_value(), "bad limited result");
        require(result.reason == "node_limit", "wrong stop reason");
    });
    test("completed shorter horizons survive interruption", [] {
        const auto first = checked(Board(), limits());
        auto budget = limits(16);
        budget.nodes = first.nodes + 1;
        const auto result = checked(Board(), budget);
        require(result.status == MateStatus::unknown && result.completed_moves == 1,
                "lost completed horizon or mislabelled partial iteration");
    });
    test("preexisting stop yields unknown", [] {
        const std::atomic_bool stop{true};
        const auto result = checked(Board(), limits(16), &stop);
        require(result.status == MateStatus::unknown && result.reason == "stopped", "stop mislabelled");
    });
    test("invalid limits and root candidates rejected", [] {
        invalid([] { (void)solve_mate(Board(), limits(0)); });
        invalid([] { (void)solve_mate(Board(), limits(65)); });
        invalid([] { (void)solve_mate(Board(), only(1, "e2e5")); });
        auto budget = limits();
        budget.movetime = std::chrono::milliseconds(-1);
        invalid([&] { (void)solve_mate(Board(), budget); });
        budget = limits();
        budget.root_moves = std::vector<Move>{};
        invalid([&] { (void)solve_mate(Board(), budget); });
        budget.root_moves = std::vector<Move>{Move::from_uci("e2e4"), Move::from_uci("e2e4")};
        invalid([&] { (void)solve_mate(Board(), budget); });
    });
    std::cout << passed << " mate cases passed, " << failed << " failed\n";
    return failed ? 1 : 0;
}
