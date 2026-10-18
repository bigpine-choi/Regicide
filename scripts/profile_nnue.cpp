#include "regicide/neural.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <random>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

// Forward-inference cost only: move generation and model loading are excluded.
// A transition includes copying the parent, updating both perspectives and
// evaluating the child. This is not a playing-strength benchmark.
int main(int argc, char** argv) {
    try {
        if (argc < 2 || argc > 3) throw std::runtime_error("usage: profile_nnue MODEL [ITERATIONS]");
        const auto model = regicide::NeuralEvaluator::load(argv[1]);
        const auto iterations = argc == 3 ? std::stoull(argv[2]) : 500000ULL;
        if (!iterations || iterations > 100000000ULL) throw std::runtime_error("iterations must be 1..100000000");
        struct Transition {
            regicide::Position before;
            regicide::Board after;
            regicide::NeuralEvaluator::Accumulator parent;
        };
        std::vector<Transition> corpus;
        int static_disagreements = 0, max_abs_cp_delta = 0;
        std::mt19937 rng(20261019);
        regicide::Board board;
        for (int index = 0; index < 256; ++index) {
            if (board.outcome() != regicide::Outcome::ongoing || board.history_size() >= 80)
                board = regicide::Board();
            const auto moves = board.legal_moves();
            if (moves.empty()) throw std::runtime_error("empty benchmark position");
            const auto parent = model->refresh(board);
            const auto before = board.position();
            board.play_generated(moves[rng() % moves.size()]);
            const auto child = model->update(parent, before, board.position());
            if (child != model->refresh(board)) throw std::runtime_error("accumulator parity failure");
            const int delta = std::abs(model->evaluate(board) - model->evaluate_nnue(board, child));
            static_disagreements += delta != 0;
            max_abs_cp_delta = std::max(max_abs_cp_delta, delta);
            corpus.push_back({before, board, parent});
        }
        const auto measure = [&](int mode) {
            std::int64_t checksum = 0;
            const auto start = std::chrono::steady_clock::now();
            for (std::uint64_t index = 0; index < iterations; ++index) {
                const auto& transition = corpus[index % corpus.size()];
                if (mode == 0) checksum += model->evaluate(transition.after);
                else if (mode == 1) checksum += model->evaluate_nnue_full(transition.after);
                else {
                    const auto child = model->update(transition.parent, transition.before,
                                                     transition.after.position());
                    checksum += model->evaluate_nnue(transition.after, child);
                }
            }
            const auto ns = std::chrono::duration<double, std::nano>(
                std::chrono::steady_clock::now() - start).count();
            return std::pair{ns / static_cast<double>(iterations), checksum};
        };
        // Rotate ordering across three rounds and report medians.
        std::vector<double> times[3];
        std::int64_t checksums[3]{};
        for (int round = 0; round < 3; ++round) {
            for (int offset = 0; offset < 3; ++offset) {
                const int mode = (round + offset) % 3;
                const auto [ns, checksum] = measure(mode);
                times[mode].push_back(ns);
                checksums[mode] = checksum;
            }
        }
        if (checksums[1] != checksums[2]) throw std::runtime_error("quantized checksum mismatch");
        const char* labels[] = {"legacy_full", "quantized_full", "nnue_update_and_evaluate"};
        std::cout << "{\"iterations\":" << iterations << ",\"positions\":" << corpus.size()
                  << ",\"static_disagreements\":" << static_disagreements
                  << ",\"max_abs_cp_delta\":" << max_abs_cp_delta;
        for (int mode = 0; mode < 3; ++mode) {
            std::sort(times[mode].begin(), times[mode].end());
            std::cout << ",\"" << labels[mode] << "\":{\"ns_per_evaluation\":"
                      << times[mode][1] << ",\"checksum\":" << checksums[mode] << '}';
        }
        std::cout << "}\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return EXIT_FAILURE;
    }
}
