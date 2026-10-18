# Independent learning-cycle audit

Status: **passed**; 527,059 assertions.

This audit ran after the guarded arena ended. It launched no engine and imported no learning implementation helpers.

- PASS frozen identities and epoch selection (59 assertions)
- PASS source/corpus games, labels and PGNs (398,893 assertions)
- PASS same-root loss diagnoses and train corrections (27,199 assertions)
- PASS fixed holdouts and source provenance (1,280 assertions)
- PASS independent paired arena replay and statistics (88,671 assertions)
- PASS native resource evidence and registry decision (10,952 assertions)
- PASS prior invalid arenas excluded; data and candidate models preserved (5 assertions)

```json
{
  "training_selected_epoch": 8,
  "model_sha256": "4b9dc188bb289d83f6563e9a996c27eaca4aef886c3421a8125c06dd108c3256",
  "attempt_index": 2,
  "validated_sample_pv_plies": 11624,
  "datasets": {
    "bootstrap": {
      "games": 32,
      "samples": {
        "train": 367,
        "test": 125,
        "validation": 96
      }
    },
    "runs/000001-88bbee0e1ae3/data": {
      "games": 32,
      "samples": {
        "test": 95,
        "train": 294,
        "validation": 88
      }
    },
    "runs/000002-95a3500d5501/data": {
      "games": 32,
      "samples": {
        "train": 325,
        "validation": 82,
        "test": 118
      }
    },
    "runs/000002-95a3500d5501/corpus": {
      "games": 71,
      "samples": {
        "train": 979,
        "validation": 96,
        "test": 125
      }
    }
  },
  "diagnostic_pv_plies": 1656,
  "diagnosed_positions": 144,
  "accepted_loss_corrections": 12,
  "fixed_holdout_rows": {
    "validation": 96,
    "test": 125
  },
  "arena_pv_plies": 27414,
  "arena_games": 64,
  "arena_pairs": 32,
  "arena_plies": 5945,
  "arena_candidate_outcomes": {
    "losses": 46,
    "draws": 10,
    "wins": 8
  },
  "candidate_pair_score": 0.203125,
  "independent_alpha": 0.008333333333333333,
  "independent_lower_bound": 0,
  "independent_promotion_passed": false,
  "maximum_response_seconds": 0.103462,
  "resource_samples": 1096,
  "maximum_resource_gap_seconds": 0.529205600003479,
  "adopted": false,
  "retained_model_sha256": null,
  "previous_invalid_attempts_preserved_and_excluded": [
    {
      "attempt_index": 1,
      "played_games": 34,
      "status": "failed",
      "adopted": false,
      "resource_failure": {
        "utc": "2026-10-18T13:59:49.462032+00:00",
        "reason": "CPU sampling gap exceeded the fail-closed 2 second bound",
        "kind": "sampling_gap",
        "retry_allowed": true
      },
      "retained_dataset_manifest_sha256": "7abb0d946e776d7410852a5d274b399b5c9e3b30e20660bc4961aafd9aeb3d4b",
      "retained_candidate_sha256": "9d6fe66578718042edd18023ffd6fa812daed5457f03df218ffed27aa6701a69"
    }
  ]
}
```

Verifies saved evidence and consistency, not universal strength; external workload observation is limited to named processes sampled by the original guard
