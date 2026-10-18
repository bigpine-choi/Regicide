#include "regicide/neural.hpp"
#include "reference_neural_v063.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

// Compile this driver, the frozen evaluator and the production evaluator as
// separate translation units WITHOUT LTO. The external calls then cannot be
// hoisted out of the timing loop. Model loading, FEN parsing, score parity,
// calibration and output are excluded from reported samples.
namespace {
using Clock = std::chrono::steady_clock;
constexpr std::uint64_t warmup_calls = 10000;
constexpr std::size_t block_count = 9;
constexpr std::array<const char*, 2> variant_names{{"old_v063", "new"}};
constexpr std::array<std::array<std::size_t, 2>, 2> orders{{{{0, 1}}, {{1, 0}}}};
constexpr std::array<const char*, 2> order_names{{"AB", "BA"}};
// Each block has the same number of AB and BA adjacent pairs. Odd blocks
// reverse the four-pair sequence, balancing order against gradual drift.
constexpr std::array<std::size_t, 4> order_sequence{{0, 1, 1, 0}};

struct Options {
    std::vector<std::string> models;
    std::string fen_file;
    std::string output;
    bool verify_only = false;
    std::uint64_t calls = 0;
    double target_ms = 150.0;
};

struct Evaluators {
    std::shared_ptr<const regicide::reference_v063::NeuralEvaluator> old;
    std::shared_ptr<const regicide::NeuralEvaluator> current;
};

struct Sample {
    std::int64_t elapsed_ns = 0;
    std::int64_t checksum = 0;
    double ns_per_call = 0;
};

struct RecordedSample {
    std::size_t sequence;
    std::size_t block;
    std::size_t pair;
    std::size_t order;
    std::size_t variant;
    Sample value;
};

struct ModelRun {
    std::string path;
    Evaluators models;
    std::size_t mismatches = 0;
    std::string differences;
    std::array<std::int64_t, 2> warmup_checksums{};
    Sample calibration{};
    std::uint64_t calibration_calls = 0;
    std::uint64_t calls = 0;
    std::array<std::vector<double>, 2> block_means;
    std::array<std::int64_t, 2> expected_checksums{};
    bool checksums_match = true;
    std::vector<RecordedSample> samples;
};

std::string json_string(const std::string& value) {
    std::ostringstream output;
    output << '"';
    for (const unsigned char character : value) {
        if (character == '"' || character == '\\') output << '\\' << character;
        else if (character < 0x20)
            output << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                   << static_cast<unsigned int>(character) << std::dec;
        else output << character;
    }
    output << '"';
    return output.str();
}

Options parse_options(int argc, char** argv) {
    Options result;
    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        if (argument == "--verify-only") {
            result.verify_only = true;
            continue;
        }
        if (index + 1 == argc) throw std::invalid_argument("Missing value for " + argument);
        const std::string value = argv[++index];
        if (argument == "--model") result.models.push_back(value);
        else if (argument == "--fen-file") result.fen_file = value;
        else if (argument == "--output") result.output = value;
        else if (argument == "--calls") {
            std::size_t parsed = 0;
            result.calls = std::stoull(value, &parsed);
            if (parsed != value.size() || value.front() == '-' ||
                result.calls == 0 || result.calls > 1000000000ULL)
                throw std::invalid_argument("--calls must be 1..1000000000");
        } else if (argument == "--target-ms") {
            std::size_t parsed = 0;
            result.target_ms = std::stod(value, &parsed);
            if (parsed != value.size() || !std::isfinite(result.target_ms) ||
                result.target_ms < 10 || result.target_ms > 1000)
                throw std::invalid_argument("--target-ms must be 10..1000");
        } else throw std::invalid_argument("Unknown option " + argument);
    }
    if (result.models.empty()) throw std::invalid_argument("At least one --model is required");
    if (result.fen_file.empty()) throw std::invalid_argument("--fen-file is required");
    return result;
}

std::vector<regicide::Board> load_boards(const std::string& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("Cannot open FEN file " + path);
    std::vector<regicide::Board> result;
    std::string line;
    std::size_t line_number = 0;
    while (std::getline(input, line)) {
        ++line_number;
        const auto first = line.find_first_not_of(" \t\r");
        if (first == std::string::npos || line[first] == '#') continue;
        const auto last = line.find_last_not_of(" \t\r");
        try {
            result.emplace_back(line.substr(first, last - first + 1));
        } catch (const std::exception& error) {
            throw std::runtime_error("FEN line " + std::to_string(line_number) + ": " + error.what());
        }
    }
    if (input.bad()) throw std::runtime_error("Failed reading FEN corpus");
    if (result.empty()) throw std::runtime_error("FEN corpus is empty");
    return result;
}

// The branch is outside the timer and the loop. Both direct method calls are
// external to this translation unit, with identical corpus order and counts.
template <typename Evaluator>
Sample measure_model(const Evaluator& model, const std::vector<regicide::Board>& boards,
                     std::uint64_t calls) {
    std::size_t index = 0;
    std::int64_t checksum = 0;
    const auto start = Clock::now();
    for (std::uint64_t call = 0; call < calls; ++call) {
        checksum += model.evaluate(boards[index], true, true);
        if (++index == boards.size()) index = 0;
    }
    const auto elapsed = std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - start).count();
    if (elapsed <= 0) throw std::runtime_error("Clock did not advance");
    return {elapsed, checksum, static_cast<double>(elapsed) / static_cast<double>(calls)};
}

Sample measure(const Evaluators& models, std::size_t variant,
               const std::vector<regicide::Board>& boards, std::uint64_t calls) {
    return variant == 0 ? measure_model(*models.old, boards, calls) :
                          measure_model(*models.current, boards, calls);
}

double median(std::vector<double> values) {
    std::sort(values.begin(), values.end());
    const auto middle = values.size() / 2;
    return values.size() % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
}

void output_json(const std::string& text, const std::string& path) {
    if (!path.empty()) {
        std::ofstream output(path, std::ios::binary);
        if (!output) throw std::runtime_error("Cannot write output file " + path);
        output << text;
        if (!output) throw std::runtime_error("Failed writing output file " + path);
    }
    std::cout << text;
}

int run(const Options& options) {
    if (!options.output.empty() && std::filesystem::exists(options.output))
        throw std::runtime_error("Output already exists; choose a new output path");
    const auto boards = load_boards(options.fen_file);
    std::vector<ModelRun> runs;
    bool all_match = true;
    for (const auto& path : options.models) {
        ModelRun model;
        model.path = path;
        model.models = {regicide::reference_v063::NeuralEvaluator::load(path),
                        regicide::NeuralEvaluator::load(path)};
        std::ostringstream differences;
        for (std::size_t position = 0; position < boards.size(); ++position) {
            for (const bool shield : {false, true}) for (const bool structure : {false, true}) {
                const int old_score = model.models.old->evaluate(boards[position], shield, structure);
                const int new_score = model.models.current->evaluate(boards[position], shield, structure);
                if (old_score == new_score) continue;
                if (model.mismatches++) differences << ',';
                differences << "{\"position\":" << position << ",\"shield\":"
                            << (shield ? "true" : "false") << ",\"structure\":"
                            << (structure ? "true" : "false") << ",\"old\":" << old_score
                            << ",\"new\":" << new_score << '}';
            }
        }
        model.differences = differences.str();
        all_match = all_match && model.mismatches == 0;
        runs.push_back(std::move(model));
    }
    const auto board_count = static_cast<std::uint64_t>(boards.size());
    std::vector<std::vector<std::size_t>> model_orders;
    const bool timed = !options.verify_only && all_match;
    if (timed) {
        for (auto& model : runs) {
            for (std::size_t variant = 0; variant < 2; ++variant)
                model.warmup_checksums[variant] = measure(model.models, variant, boards, warmup_calls).checksum;
            if (model.warmup_checksums[0] != model.warmup_checksums[1])
                throw std::runtime_error("Warmup checksum mismatch");
            model.calls = options.calls;
            if (!model.calls) {
                // Only the frozen baseline chooses work, independently per
                // model. Every pair uses identical complete-corpus counts.
                model.calibration_calls = ((100000ULL + board_count - 1) / board_count) * board_count;
                model.calibration = measure(model.models, 0, boards, model.calibration_calls);
                const double estimate = std::ceil(static_cast<double>(model.calibration_calls) * options.target_ms *
                                                  1000000 / static_cast<double>(model.calibration.elapsed_ns));
                const double bounded = std::clamp(estimate, static_cast<double>(board_count), 999000000.0);
                model.calls = ((static_cast<std::uint64_t>(bounded) + board_count - 1) / board_count) * board_count;
            }
        }
        std::size_t sequence = 0;
        for (std::size_t block = 0; block < block_count; ++block) {
            std::vector<std::size_t> model_order;
            for (std::size_t rank = 0; rank < runs.size(); ++rank)
                model_order.push_back((block / 2 + (block % 2 ? runs.size() - 1 - rank : rank)) % runs.size());
            model_orders.push_back(model_order);
            for (const auto index : model_order) {
                auto& model = runs[index];
                std::array<double, 2> sums{};
                for (std::size_t pair = 0; pair < order_sequence.size(); ++pair) {
                    const auto order = order_sequence[pair] ^ (block % 2);
                    for (const auto variant : orders[order]) {
                        const auto sample = measure(model.models, variant, boards, model.calls);
                        if (block || pair) model.checksums_match = model.checksums_match &&
                            sample.checksum == model.expected_checksums[variant];
                        model.expected_checksums[variant] = sample.checksum;
                        sums[variant] += sample.ns_per_call;
                        model.samples.push_back({sequence++, block, pair, order, variant, sample});
                    }
                }
                model.checksums_match = model.checksums_match &&
                    model.expected_checksums[0] == model.expected_checksums[1];
                for (std::size_t variant = 0; variant < 2; ++variant)
                    model.block_means[variant].push_back(sums[variant] / order_sequence.size());
                all_match = all_match && model.checksums_match;
            }
        }
    }
    std::ostringstream output;
    output << std::setprecision(15)
           << "{\n  \"schema\":\"regicide-neural-profile-v1\",\n"
           << "  \"frozen_source_commit\":\"00dec89b08de4e5f573b18b440a93cd82f4b3311\",\n"
           << "  \"fen_filename\":" << json_string(std::filesystem::path(options.fen_file).filename().string()) << ",\n"
           << "  \"mode\":\"" << (options.verify_only ? "verify" : "profile") << "\",\n"
           << "  \"positions\":" << boards.size() << ",\n"
           << "  \"scope\":\"full_static_evaluation_including_V1_material_or_V2_classical_evaluation\",\n"
           << "  \"timed_options\":{\"shield\":true,\"structure\":true},\n"
           << "  \"blocks\":" << (timed ? block_count : 0) << ",\n"
           << "  \"pairs_per_block\":4,\n"
           << "  \"orders_even_blocks\":[\"AB\",\"BA\",\"BA\",\"AB\"],\n"
           << "  \"orders_odd_blocks\":[\"BA\",\"AB\",\"AB\",\"BA\"],\n"
           << "  \"variant_order\":[\"old_v063\",\"new\"],\n"
           << "  \"model_order_per_block\":[";
    for (std::size_t block = 0; block < model_orders.size(); ++block) {
        if (block) output << ',';
        output << '[';
        for (std::size_t rank = 0; rank < model_orders[block].size(); ++rank) {
            if (rank) output << ',';
            output << model_orders[block][rank];
        }
        output << ']';
    }
    output << "],\n  \"models\":[\n";
    for (std::size_t index = 0; index < runs.size(); ++index) {
        if (index) output << ",\n";
        const auto& model = runs[index];
        output << "    {\"model_index\":" << index << ",\"model_filename\":"
               << json_string(std::filesystem::path(model.path).filename().string())
               << ",\"equivalence\":{\"option_combinations_per_position\":4,\"comparisons\":"
               << boards.size() * 4 << ",\"mismatch_count\":" << model.mismatches
               << ",\"mismatches\":[" << model.differences << "]}";
        if (timed) {
            output << ",\"warmup_calls_per_variant\":" << warmup_calls
                   << ",\"warmup_checksums\":[" << model.warmup_checksums[0] << ',' << model.warmup_checksums[1] << ']'
                   << ",\"calls_per_sample\":" << model.calls
                   << ",\"calibration\":{\"variant\":\"old_v063\",\"calls\":" << model.calibration_calls
                   << ",\"elapsed_ns\":" << model.calibration.elapsed_ns << ",\"checksum\":" << model.calibration.checksum
                   << ",\"target_ms\":" << options.target_ms << "},\"samples\":[\n";
            for (std::size_t sample_index = 0; sample_index < model.samples.size(); ++sample_index) {
                if (sample_index) output << ",\n";
                const auto& sample = model.samples[sample_index];
                output << "      {\"sequence\":" << sample.sequence << ",\"block\":" << sample.block
                       << ",\"pair\":" << sample.pair << ",\"order\":\"" << order_names[sample.order]
                       << "\",\"variant\":\"" << variant_names[sample.variant]
                       << "\",\"elapsed_ns\":" << sample.value.elapsed_ns
                       << ",\"ns_per_call\":" << sample.value.ns_per_call
                       << ",\"checksum\":" << sample.value.checksum << '}';
            }
            output << "\n    ],\"timed_checksums_match\":" << (model.checksums_match ? "true" : "false")
                   << ",\"block_means_ns_per_call\":[\n";
            std::vector<double> ratios;
            for (std::size_t block = 0; block < block_count; ++block) {
                if (block) output << ",\n";
                const auto ratio = model.block_means[1][block] / model.block_means[0][block];
                ratios.push_back(ratio);
                output << "      {\"block\":" << block << ",\"old_v063\":" << model.block_means[0][block]
                       << ",\"new\":" << model.block_means[1][block] << ",\"new_over_old\":" << ratio << '}';
            }
            output << "\n    ],\"medians\":{\"old_v063_ns\":" << median(model.block_means[0])
                   << ",\"new_ns\":" << median(model.block_means[1])
                   << ",\"paired_new_over_old\":" << median(ratios) << '}'
                   << ",\"block_ratio_min\":" << *std::min_element(ratios.begin(), ratios.end())
                   << ",\"block_ratio_max\":" << *std::max_element(ratios.begin(), ratios.end());
        }
        output << '}';
    }
    output << "\n  ],\n  \"all_scores_and_checksums_match\":" << (all_match ? "true" : "false") << "\n}\n";
    output_json(output.str(), options.output);
    return all_match ? 0 : 1;
}
} // namespace

int main(int argc, char** argv) {
    try {
        return run(parse_options(argc, argv));
    } catch (const std::exception& error) {
        std::cerr << "profile_neural: " << error.what() << '\n';
        return 2;
    }
}
