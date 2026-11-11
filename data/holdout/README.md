# Held-out classification set (brightwater-holdout-v1)

This folder has five extra documents for the same fictional customer (Brightwater Coffee Co.), written after the classifier was tuned. They are used to test the Jev classifier on documents it was never tuned on. `labels.json` holds each document's class and a one-line reason for it.

**Do not tune on this set.** Leave the classifier prompt, criteria and `NOISE_THRESHOLD` as they are after looking at these results. Otherwise the set stops being held out. To tune, add the documents to `data/synthetic/` and write a new holdout set.

The vendors (Pinegrove, Tidewater, Brightline) are new and don't appear in `data/synthetic/`. Every domain uses `.example`. The set leans toward hard cases:
- a price increase buried in a newsletter (h1)
- a vendor product-launch email that quotes prices (h4)
- an internal email that mentions spending (h5)

```
uv run python -m leo.eval.holdout                  # Jev, 5 calls (needs JEV_API_KEY)
uv run python -m leo.eval.holdout --json PATH      # also write the full result
```

"Matters" here means the key class isn't noise, because this set has no planted findings. With five documents, the metrics are directional only. Results are in `results/`.
