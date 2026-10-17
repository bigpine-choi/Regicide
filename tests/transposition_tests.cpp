#include "regicide/transposition.hpp"
#include "regicide/search.hpp"

#include <bit>
#include <functional>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

namespace regicide {
struct TranspositionTableTestAccess {
    static void generation(TranspositionTable& table, std::uint32_t generation) {
        table.generation_ = generation;
    }
};
}

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
    test("same-size prepare reuses backing memory but no old entries", [] {
        TranspositionTable table;
        expect(table.capacity() == 0 && table.allocated_bytes() == 0,
               "default table must not preallocate");
        expect(!table.clear() && !table.probe({}) &&
                   !table.store({}, 1, 0, 0, TranspositionBound::exact, {}),
               "unallocated table must remain safe and empty");
        const auto allocated = table.prepare(1);
        expect(allocated.allocations == 1 && allocated.reuses == 0 && allocated.clears == 0 &&
                   allocated.bytes == table.allocated_bytes(), "first prepare must allocate once");
        const TranspositionKey original{};
        const TranspositionKey collision{std::rotl(UINT64_C(1), 23), 1, 0};
        table.store(original, 64, mate_score - 3, 0, TranspositionBound::exact, Move::from_uci("e2e4"));
        const auto* address = table.probe(original);
        expect(address != nullptr, "first search must store entries");
        const auto reused = table.prepare(1);
        expect(reused.allocations == 0 && reused.reuses == 1 && reused.clears == 0 &&
                   reused.bytes == allocated.bytes, "same-size prepare must avoid allocation and full clear");
        expect(!table.probe(original), "old scores and moves must be invisible in the next search");
        expect(table.store(collision, 1, 10, 0, TranspositionBound::upper, Move::from_uci("d2d4")),
               "deep stale entry must not block a shallow new-generation collision");
        expect(table.probe(collision) == address, "same-size search must retain the actual backing address");
        expect(!table.clear() && !table.probe(collision), "explicit clear must logically invalidate without a sweep");
        expect(table.allocated_bytes() == allocated.bytes, "explicit clear must retain bounded storage");
        expect(table.store(original, 1, 42, 0, TranspositionBound::exact, {}) &&
                   table.probe(original) == address, "cleared storage must be reusable");
    });
    test("resizing changes storage budget and invalid arguments preserve a table", [] {
        TranspositionTable table;
        const auto one = table.prepare(1);
        table.store({}, 4, 70, 0, TranspositionBound::exact, {});
        const auto two = table.prepare(2);
        expect(two.allocations == 1 && two.reuses == 0 && two.clears == 0 &&
                   two.bytes > one.bytes && two.bytes <= 2 * 1024 * 1024,
               "growing table must allocate the new requested budget");
        expect(!table.probe({}), "resizing must discard old entries");
        table.store({}, 8, 80, 0, TranspositionBound::exact, {});
        const auto shrunk = table.prepare(1);
        expect(shrunk.allocations == 1 && shrunk.reuses == 0 && shrunk.bytes == one.bytes,
               "shrinking must release the old larger capacity");
        expect(!table.probe({}), "shrinking must not expose old larger-table entries");
        table.store({}, 2, 90, 0, TranspositionBound::exact, {});
        const auto* original = table.probe({});
        rejects([&] { (void)table.prepare(0); });
        rejects([&] { (void)table.prepare(max_hash_mb + 1); });
        expect(table.probe({}) == original && table.probe({})->score == 90 &&
                   table.allocated_bytes() == one.bytes, "rejected resize must preserve the previous table");
    });
    test("generation wrap physically removes old generation-one records once", [] {
        TranspositionTable table(1);
        table.store({}, 64, 123, 0, TranspositionBound::exact, Move::from_uci("a2a4"));
        const auto* address = table.probe({});
        const auto bytes = table.allocated_bytes();
        TranspositionTableTestAccess::generation(table, std::numeric_limits<std::uint32_t>::max() - 1);
        expect(!table.clear() && !table.probe({}), "last non-wrapping clear must stay logical");
        // The generation-one record remains physically present until wrap.
        expect(address->generation == 1 && address->depth == 64, "fixture needs a stale generation-one record");
        const auto wrapped = table.prepare(1);
        expect(wrapped.allocations == 0 && wrapped.reuses == 1 && wrapped.clears == 1 &&
                   wrapped.bytes == bytes, "wrap must sweep once while retaining allocation");
        expect(!table.probe({}) && address->generation == 0 && address->depth == -1,
               "wrapped generation must never resurrect an ancient score or move");
        expect(table.store({}, 1, 5, 0, TranspositionBound::exact, {}) && table.probe({}) == address,
               "physically cleared storage must remain usable");
        const auto next = table.prepare(1);
        expect(next.allocations == 0 && next.reuses == 1 && next.clears == 0 && !table.probe({}),
               "ordinary search after wrap must return to constant-time invalidation");
    });
    std::cout << groups << " groups, " << assertions << " assertions, " << failures << " failures\n";
    return failures ? 1 : 0;
}
