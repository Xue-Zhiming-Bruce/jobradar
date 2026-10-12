"""Matching a posting against the resume: the prefilter, then the scorer's seam.

Two modules, in the order #9's tick uses them: `prefilter` decides what is worth
scoring without a model, `scorer` holds the `MatchScorer` interface and the fake
the suite scores with. The real scorer -- the prompt, the model call, the parsing
and the retries -- is #8's and is deliberately not here: this package imports no
LLM client and no HTTP client.
"""
