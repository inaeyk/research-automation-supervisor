# Context Economy V1 external replay

Do not start the A/B replay from an implementation Codex task. A human operator should run
the baseline and candidate as separate, frozen campaigns after this change passes validation.

1. Freeze identical campaign inputs, repository commits, model, reasoning effort, and acceptance
   authority. PA-5D remains paused and is not replay authority.
2. Run the baseline and B4 candidate externally, preserving each complete artifact tree. Do not
   reuse a session across the A/B arms.
3. Require identical correctness, recovery, exactly-once, and acceptance outcomes before comparing
   economy metrics. Treat an incomplete usage receipt as a failed measurement.
4. Produce the deterministic comparison without launching Codex:

   ```bash
   .venv/bin/python scripts/context_economy_replay_report.py \
     --baseline /absolute/path/to/baseline-artifacts \
     --candidate /absolute/path/to/candidate-artifacts
   ```

Compare total and uncached input, output, tool calls, model-visible tool-output characters,
compactions, and maximum/median inference input. Inspect every recorded override. The report is
descriptive; it never changes scientific authority or declares an A/B winner.
