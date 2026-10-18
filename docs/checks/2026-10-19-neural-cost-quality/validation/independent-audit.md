# Independent audit: neural cost, decisions and teacher quality

Passed: true. Checks: 537,977.

Raw evidence was checked after all experiments finished using separate arithmetic and legal chess replay. No engine search or model adoption was executed.

- Frozen implementation, history, attempt and derived artifacts: PASS (191 checks)
- Train-only teacher ladder, original provenance and immutable holdouts: PASS (394,938 checks)
- Zero-head control, exact work, balanced schedule and independent cost: PASS (22,118 checks)
- Conditional finite teacher ranks and regret: PASS (2,054 checks)
- Full arena rules, resource evidence, independent promotion criterion: PASS (118,233 checks)
- Learning, development diagnostic and reserved-opening separation: PASS (440 checks)

Agreement between finite searches by the own engine and ranks among compared moves are limited diagnostic evidence. They do not establish ground truth or increased playing strength.
The resource audit checks sampled CPU records for recognized processes. It does not exclude all host workloads or temperature effects.
