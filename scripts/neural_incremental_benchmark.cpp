#include "regicide/evaluation.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {
using namespace regicide;

struct Transition {
    Board before;
    Board after;
    Move move;
    NeuralAccumulator before_accumulator;
    NeuralAccumulator after_accumulator;
    NeuralAccumulator working_accumulator;
};

std::vector<Transition> make_transitions() {
    // Developer-composed positions: every legal child is included, without
    // looking at measured timings. Both colors and all special move types occur.
    constexpr std::array<std::string_view, 9> fens{
        Board::start_fen,
        "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
        "r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1",
        "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
        "7k/8/8/8/3Pp3/8/8/6K1 b - d3 0 1",
        "1r5k/P7/8/8/8/8/8/6K1 w - - 0 1",
        "6k1/8/8/8/8/8/p7/1R5K b - - 0 1",
        "7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1",
        "8/6k1/5pp1/3p4/3P4/4P1P1/5PK1/8 w - - 0 40"};
    std::vector<Transition> result;
    for (auto fen : fens) {
        const Board before(fen);
        const NeuralAccumulator initial(before);
        for (Move move : before.legal_moves()) {
            Board after = before;
            after.play_generated(move);
            NeuralAccumulator updated = initial;
            updated.update_generated(before.position(), after.position(), move);
            const NeuralAccumulator refreshed(after);
            if (updated != refreshed || updated.evaluate(after.position().turn) != evaluate_neural(after) ||
                updated.evaluate(Color::white) != refreshed.evaluate(Color::white) ||
                updated.evaluate(Color::black) != refreshed.evaluate(Color::black))
                throw std::runtime_error("preflight incremental/full score mismatch");
            result.push_back({before, after, move, initial, updated, initial});
            // Reserve any Board history buffers before timing play/undo paths.
            result.back().before.play_generated(move);
            result.back().before.undo();
            if (result.back().before != before)
                throw std::runtime_error("preflight board undo mismatch");
        }
    }
    if (result.empty()) throw std::runtime_error("empty transition suite");
    return result;
}

enum class Workload { classical, full, score_only, update, board_full, board_incremental };
constexpr std::array workloads{Workload::classical, Workload::full, Workload::score_only,
    Workload::update, Workload::board_full, Workload::board_incremental};
constexpr std::array names{"classical_after", "neural_full_after", "neural_score_only",
    "neural_update_snapshot_restore", "board_play_full_undo", "board_play_incremental_undo"};

// Each trial follows the same deterministic runtime-dependent access order.
// Evaluation/update calls live in another translation unit. Signal fences and
// the printed checksum keep the timed reads and results observable to compilers.
template<Workload workload>
std::int64_t execute(std::vector<Transition>& transitions, int iterations) {
    std::int64_t checksum = 0;
    std::uint32_t state = 0x6a09e667U;
    for (int i = 0; i < iterations; ++i) {
        state = state * 1664525U + 1013904223U;
        auto& item = transitions[state % transitions.size()];
        std::atomic_signal_fence(std::memory_order_seq_cst);
        if constexpr (workload == Workload::classical) {
            checksum += evaluate_position(item.after);
        } else if constexpr (workload == Workload::full) {
            checksum += evaluate_neural(item.after);
        } else if constexpr (workload == Workload::score_only) {
            checksum += item.after_accumulator.evaluate(item.after.position().turn);
        } else if constexpr (workload == Workload::update) {
            const auto saved = item.working_accumulator;
            item.working_accumulator.update_generated(item.before.position(), item.after.position(), item.move);
            checksum += item.working_accumulator.evaluate(item.after.position().turn);
            item.working_accumulator = saved;
        } else if constexpr (workload == Workload::board_full) {
            item.before.play_generated(item.move);
            checksum += evaluate_neural(item.before);
            item.before.undo();
        } else if constexpr (workload == Workload::board_incremental) {
            const auto before = item.before.position();
            const auto saved = item.working_accumulator;
            item.before.play_generated(item.move);
            item.working_accumulator.update_generated(before, item.before.position(), item.move);
            checksum += item.working_accumulator.evaluate(item.before.position().turn);
            item.before.undo();
            item.working_accumulator = saved;
        }
        std::atomic_signal_fence(std::memory_order_seq_cst);
    }
    return checksum;
}

using Runner = std::int64_t (*)(std::vector<Transition>&, int);
constexpr std::array<Runner, 6> runners{execute<Workload::classical>, execute<Workload::full>,
    execute<Workload::score_only>, execute<Workload::update>, execute<Workload::board_full>,
    execute<Workload::board_incremental>};

int positive(std::string_view text) {
    int value = 0;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
    if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size() || value <= 0)
        throw std::invalid_argument("iterations/repeats must be positive integers");
    return value;
}
} // namespace

int main(int argc, char** argv) {
    try {
        if (argc > 3) throw std::invalid_argument("usage: benchmark [iterations=500000] [repeats=6]");
        const int iterations = argc > 1 ? positive(argv[1]) : 500000;
        const int repeats = argc > 2 ? positive(argv[2]) : 6;
        auto transitions = make_transitions();
        std::cout << "round,order,workload,transitions,iterations,elapsed_ns,checksum\n";
        for (int round = 0; round < repeats; ++round) {
            std::array<std::int64_t, workloads.size()> checksums{};
            // Reverse order on alternate rounds, balancing first/last positions.
            for (std::size_t order = 0; order < workloads.size(); ++order) {
                const auto index = round % 2 == 0 ? order : workloads.size() - 1 - order;
                const auto run = runners[index];
                const auto warmup_checksum = run(transitions, std::min(iterations, 1000));
                std::atomic_signal_fence(std::memory_order_seq_cst);
                const auto started = std::chrono::steady_clock::now();
                const auto checksum = run(transitions, iterations);
                const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                    std::chrono::steady_clock::now() - started).count();
                checksums[index] = checksum;
                // Warmup results are consumed outside the timed region.
                if (warmup_checksum != runners[index](transitions, std::min(iterations, 1000)))
                    throw std::runtime_error("workload is not deterministic");
                std::cout << round << ',' << order << ',' << names[index] << ',' << transitions.size()
                          << ',' << iterations << ',' << ns << ',' << checksum << '\n';
            }
            for (std::size_t i = 2; i < checksums.size(); ++i)
                if (checksums[i] != checksums[1]) throw std::runtime_error("workload checksums differ");
        }
        for (const auto& item : transitions) {
            if (item.working_accumulator != item.before_accumulator)
                throw std::runtime_error("postflight accumulator restoration mismatch");
            auto expected = item.after;
            expected.undo();
            if (item.before != expected) throw std::runtime_error("postflight board restoration mismatch");
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "benchmark failed: " << error.what() << '\n';
        return 1;
    }
}
