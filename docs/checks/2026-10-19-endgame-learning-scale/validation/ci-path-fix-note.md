# Deferred Windows CI assertion correction

Apply only after attempt 5 has completed its unchanged validation gate. No tracked source or test was modified while the attempt was frozen.

Source commit: b5f0af8713971f13d1da202eb62dee016d0c80f2.
CI run: 39148965649. Ubuntu job 117738499742 succeeded; Windows job 117738499564 failed at tests/endgame_matches_tests.py:143.

The runner's temporary directory uses the Windows 8.3 alias `C:\Users\RUNNER~1`; run_matches resolves its output directory to the equivalent `C:\Users\runneradmin` path. The assertion compared their raw strings.

Expected correction:

```diff
- self.assertEqual(engines[1].configurations[0], {"EvalFile": str(files[-1] / "candidate.nn")})
+ self.assertEqual(engines[1].configurations[0], {"EvalFile": str((files[-1] / "candidate.nn").resolve())})
```

This preserves the exact expected file and changes only normalization of the assertion's expected path. It does not modify engine evaluation, search, training, candidate selection, resource guards, or adoption criteria. Keep the original CI failure and experiment source hashes in the final report, then verify the final documentation/test commit in fresh Windows and Ubuntu CI.
