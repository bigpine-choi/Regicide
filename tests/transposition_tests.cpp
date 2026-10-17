#include "regicide/transposition.hpp"
#include "regicide/search.hpp"

#include <bit>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace regicide;
namespace {
int groups = 0, assertions = 0, failures = 0;
void expect(bool condition, const std::string& message) {
    ++assertions;
    if (!condition) throw std::runtime_error(message);
}
void test(const std::string& name, const std::function<void()>& action) {
    ++groups;
    try { action(); std::cout << "PASS " << name << '\n'; }
    catch (const std::exception& e) {
        ++failures;
        std::cerr << "FAIL " << name << ": " << e.what() << '\n';
    }
}
template<class F> void rejects(F action) {
    bool threw = false;
    try { action(); } catch (const std::invalid_argument&) { threw = true; }
    expect(threw, "invalid memory budget must be rejected");
}
TranspositionKey key(const Board& board) {
    return {board.position_key(), board.repetition_context_key(), board.position().halfmove};
}
}

int main() {
    test("bounded allocation and empty slots", [] {
        TranspositionTable table(1);
        constexpr std::size_t budget = 1024 * 1024;
        expect(table.capacity() > 0 && table.allocated_bytes() <= budget,
               "table must fit the specified MiB budget");
        expect(budget - table.allocated_bytes() < sizeof(TranspositionEntry),
               "all whole entries should be available");
        expect(!table.probe({}), "zero key must not match an unused slot");
        rejects([] { TranspositionTable invalid(0); });
        rejects([] { TranspositionTable invalid(-1); });
        rejects([] { TranspositionTable invalid(max_hash_mb + 1); });
    });
    test("exact, lower and upper bound windows", [] {
        TranspositionTable table(1);
        const TranspositionKey position{11, 22, 4};
        const auto move = Move::from_uci("e2e4");
        for (auto bound : {TranspositionBound::lower, TranspositionBound::upper,
                           TranspositionBound::exact}) {
            expect(table.store(position, 4, 70, 0, bound, move), "store must succeed");
            const auto* entry = table.probe(position);
            expect(entry && entry->best_move == move, "stored move and full key");
            expect(!entry->cutoff_score(3, 0, -100, 100), "different depth is ordering only");
            expect(!entry->cutoff_score(5, 0, -100, 100), "shallower data cannot answer deeper query");
            expect(entry->cutoff_score(4, 0, 70, 71).has_value() ==
                       (bound != TranspositionBound::lower), "upper bound uses alpha inclusively");
            expect(entry->cutoff_score(4, 0, 69, 70).has_value() ==
                       (bound != TranspositionBound::upper), "lower bound uses beta inclusively");
            expect(entry->cutoff_score(4, 0, -100, 100).has_value() ==
                       (bound == TranspositionBound::exact), "bound inside window cannot cut off");
        }
    });
    test("mate distance is normalized from either side", [] {
        for (int score : {0, 123, -456, mate_score - max_search_ply - 1,
                          -mate_score + max_search_ply + 1}) {
            expect(score_to_table(score, 7) == score, "ordinary scores must not shift");
            expect(score_from_table(score_to_table(score, 7), 2) == score,
                   "ordinary scores must survive another root ply");
        }
        for (int stored_ply : {0, 3, 12}) {
            for (int requested_ply : {0, 2, 9}) {
                // The node is mate in five, regardless of distance from root.
                for (int sign : {-1, 1}) {
                    const int original = sign * (mate_score - stored_ply - 5);
                    const int expected = sign * (mate_score - requested_ply - 5);
                    expect(score_from_table(score_to_table(original, stored_ply), requested_ply) == expected,
                           "mate distance changed when the same node appeared at a different ply");
                }
            }
        }
        const int boundary = mate_score - max_search_ply;
        expect(score_from_table(score_to_table(boundary, 8), 2) == boundary + 6,
               "maximum legal mate distance must be normalized");
        expect(score_from_table(score_to_table(-boundary, 8), 2) == -boundary - 6,
               "negative maximum legal mate distance must be normalized");
    });
    test("index collisions verify every key field and prefer deeper work", [] {
        TranspositionTable table(1);
        // These keys deliberately have identical mixed index inputs.
        const TranspositionKey first{};
        const TranspositionKey second{std::rotl(UINT64_C(1), 23), 1, 0};
        const TranspositionKey third{UINT64_C(0x9e3779b97f4a7c15), 0, 1};
        expect(table.store(first, 4, 100, 0, TranspositionBound::exact, {}), "initial store");
        expect(!table.probe(second) && !table.probe(third), "collision must not masquerade as a hit");
        expect(!table.store(second, 3, 200, 0, TranspositionBound::exact, {}), "shallow collision must not evict deeper work");
        expect(table.probe(first) && !table.probe(second), "rejected store must preserve original");
        expect(table.store(second, 4, 200, 0, TranspositionBound::exact, {}), "equal depth collision may evict");
        expect(!table.probe(first) && table.probe(second), "eviction still verifies the full key");
        expect(table.store(third, 5, 300, 0, TranspositionBound::exact, {}), "deeper collision must replace");
        expect(!table.probe(second) && table.probe(third), "halfmove participates in full key equality");
        expect(!table.store(third, 5, 400, 0, TranspositionBound::lower, {}), "exact score wins at equal depth");
        expect(table.probe(third)->score == 300, "weaker bound replaced exact score");
    });
    test("history and halfmove distinguish otherwise identical queries", [] {
        Board knight_g, knight_b;
        for (const char* move : {"g1f3", "g8f6", "f3g1", "f6g8"}) knight_g.play(move);
        for (const char* move : {"b1c3", "b8c6", "c3b1", "c6b8"}) knight_b.play(move);
        expect(knight_g.position_key() == knight_b.position_key() &&
                   knight_g.position().halfmove == knight_b.position().halfmove &&
                   knight_g.repetition_count() == knight_b.repetition_count(),
               "fixture must share position, clock and current repetition count");
        expect(key(knight_g) != key(knight_b), "off-path repetition counts must be part of the query");
        TranspositionTable table(1);
        table.store(key(knight_g), 4, 99, 0, TranspositionBound::exact, Move::from_uci("e2e4"));
        expect(!table.probe(key(knight_b)), "different histories must not reuse a score");
        Board imported(knight_g.fen());
        expect(key(imported) != key(knight_g), "FEN import must not inherit unknown old history");
        Board zero_clock, old_clock("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 98 1");
        expect(zero_clock.position_key() == old_clock.position_key() &&
                   zero_clock.repetition_context_key() == old_clock.repetition_context_key(),
               "fixture must differ only by halfmove clock");
        expect(key(zero_clock) != key(old_clock), "50/75-move counters must be part of the query");
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
