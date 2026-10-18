# V1 read-only diagnosis

Reserved arena evidence is diagnostic only. It must never become training labels or a hyperparameter-selection set.

```json
{
  "coverage": {
    "docs/checks/2026-10-18-neural-v1/data/positions.jsonl": {
      "rows": 588,
      "turns": {
        "w": 588
      },
      "turns_by_split": {
        "train": {
          "w": 367
        },
        "validation": {
          "w": 96
        },
        "test": {
          "w": 125
        }
      },
      "source_by_turn": {
        "regular": {
          "w": 588
        }
      }
    },
    "build/learning-cycle/runs/000001-88bbee0e1ae3/data/positions.jsonl": {
      "rows": 477,
      "turns": {
        "w": 477
      },
      "turns_by_split": {
        "train": {
          "w": 294
        },
        "validation": {
          "w": 88
        },
        "test": {
          "w": 95
        }
      },
      "source_by_turn": {
        "regular": {
          "w": 477
        }
      }
    },
    "build/learning-cycle/runs/000002-95a3500d5501/data/positions.jsonl": {
      "rows": 525,
      "turns": {
        "w": 525
      },
      "turns_by_split": {
        "train": {
          "w": 325
        },
        "validation": {
          "w": 82
        },
        "test": {
          "w": 118
        }
      },
      "source_by_turn": {
        "regular": {
          "w": 525
        }
      }
    },
    "build/learning-cycle/runs/000002-95a3500d5501/corpus/positions.jsonl": {
      "rows": 1200,
      "turns": {
        "w": 1175,
        "b": 25
      },
      "turns_by_split": {
        "train": {
          "w": 954,
          "b": 25
        },
        "validation": {
          "w": 96
        },
        "test": {
          "w": 125
        }
      },
      "source_by_turn": {
        "loss_analysis": {
          "b": 25,
          "w": 10
        },
        "regular": {
          "w": 1165
        }
      }
    }
  },
  "paired_same_root_first_search": {
    "pairs": 32,
    "node_ratio": {
      "count": 32,
      "mean": 0.8846690346643952,
      "median": 0.8836704592297697,
      "min": 0.7434594082495263,
      "max": 0.9700892268471716
    },
    "candidate_lower_depth": 8,
    "candidate_equal_depth": 18,
    "candidate_greater_depth": 6
  },
  "role_phase_material_patterns": {
    "candidate": {
      "high_20_24": {
        "decisions_with_two_plies": 772,
        "net_material_concessions_ge_300cp": 49,
        "reported_negative_mate": 0
      },
      "middle_7_19": {
        "decisions_with_two_plies": 1341,
        "net_material_concessions_ge_300cp": 119,
        "reported_negative_mate": 91
      },
      "low_0_6": {
        "decisions_with_two_plies": 453,
        "net_material_concessions_ge_300cp": 46,
        "reported_negative_mate": 43
      }
    },
    "baseline": {
      "high_20_24": {
        "decisions_with_two_plies": 771,
        "net_material_concessions_ge_300cp": 52,
        "reported_negative_mate": 0
      },
      "middle_7_19": {
        "decisions_with_two_plies": 1325,
        "net_material_concessions_ge_300cp": 74,
        "reported_negative_mate": 16
      },
      "low_0_6": {
        "decisions_with_two_plies": 451,
        "net_material_concessions_ge_300cp": 13,
        "reported_negative_mate": 3
      }
    }
  },
  "loss_material_states": {
    "losses": 46,
    "started_down_ge_300cp": 1,
    "never_down_ge_300cp": 1,
    "entered_low_material_phase": 32,
    "candidate_ever_reported_cp_ge_100": 20
  }
}
```

- Two-ply material concessions can be sound sacrifices; they are not engine-verified blunders.
- Teacher cp is searched while the NN observation is static; their cp gap is not alone an evaluation-error proof.
- Only paired first searches share exactly the same board. Later role-specific paths differ, so their aggregate node/depth rates are not controlled speed measurements.
- Material phase is not a complete strategic definition of opening/middlegame/endgame.
- No controlled ablation or new native engine search was performed by this helper.
