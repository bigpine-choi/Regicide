# Independent learning-cycle audit

Status: **passed**; 672,928 assertions.

This audit ran after the guarded arena ended. It launched no engine and imported no learning implementation helpers.

- PASS frozen identities and epoch selection (101 assertions)
- PASS source/corpus games, labels and PGNs (520,647 assertions)
- PASS same-root loss diagnoses and train corrections (41,817 assertions)
- PASS fixed holdouts and source provenance (1,649 assertions)
- PASS actual balanced-turn sampling; no synthetic turn flips (1,522 assertions)
- PASS independent V2 raw model, bounded correction, teacher-only MSE and L2 (20 assertions)
- PASS independent paired arena replay and statistics (95,553 assertions)
- PASS native resource evidence and registry decision (11,612 assertions)
- PASS prior invalid arenas excluded; data and candidate models preserved (7 assertions)

```json
{
  "unchanged_strength_gate_sha256": {
    "scripts/learning_arena.py": "7bc3e4a37c5438b1465fa0bac21784a4ce6c5c3ed5540923f360ced75ba8482f",
    "scripts/learning_registry.py": "da57e633edbe7b14d8f89a9c9dc3a8a12e2caec3c90e22881878f3f18ba3099b",
    "tests/data/learning_arena_openings_v1.json": "78e7e6aca52cee0bdcb65e938d0d5a94236b30a8790b283227ff9d435e51e4f0"
  },
  "training_selected_epoch": 61,
  "model_sha256": "1a7ec95c4062e7b448bf3582dc189a9c94c570708c98fce6882eb84ac0d717fe",
  "attempt_index": 3,
  "validated_sample_pv_plies": 15296,
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
    "runs/000003-2daceeed6656/data": {
      "games": 32,
      "samples": {
        "validation": 84,
        "train": 328,
        "test": 94
      }
    },
    "runs/000003-2daceeed6656/corpus": {
      "games": 91,
      "samples": {
        "train": 1327,
        "validation": 96,
        "test": 125
      }
    }
  },
  "diagnostic_pv_plies": 2477,
  "diagnosed_positions": 216,
  "accepted_loss_corrections": 30,
  "fixed_holdout_rows": {
    "validation": 96,
    "test": 125
  },
  "new_collection_turns": {
    "train": {
      "w": 175,
      "b": 153
    },
    "validation": {
      "w": 44,
      "b": 40
    },
    "test": {
      "w": 49,
      "b": 45
    }
  },
  "training_turns": {
    "train": {
      "w": 1139,
      "b": 188
    },
    "validation": {
      "w": 96,
      "b": 0
    },
    "test": {
      "w": 125,
      "b": 0
    }
  },
  "independent_v2_metrics": {
    "train": {
      "teacher_only_mse": 0.025292150344676075,
      "classical_only_mse": 0.042224568616639195
    },
    "validation": {
      "teacher_only_mse": 0.04103797302463677,
      "classical_only_mse": 0.04859568136537474
    },
    "test": {
      "teacher_only_mse": 0.036181570808213284,
      "classical_only_mse": 0.03648879805425692
    }
  },
  "independent_v2_l2_penalty": 0.0016317931925629253,
  "maximum_v2_correction_cp_on_corpus": 99.99093461007678,
  "arena_pv_plies": 29766,
  "arena_games": 64,
  "arena_pairs": 32,
  "arena_plies": 6308,
  "arena_candidate_outcomes": {
    "losses": 35,
    "wins": 11,
    "draws": 18
  },
  "candidate_pair_score": 0.3125,
  "independent_alpha": 0.004166666666666667,
  "independent_lower_bound": 0.019865444321388093,
  "independent_promotion_passed": false,
  "maximum_response_seconds": 0.105659,
  "resource_samples": 1162,
  "maximum_resource_gap_seconds": 0.5319078000029549,
  "adopted": false,
  "retained_model_sha256": null,
  "previous_invalid_attempts_preserved_and_excluded": [
    {
      "attempt_index": 1,
      "recorded_games": 34,
      "completed_games": 33,
      "unfinished_games": 1,
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
