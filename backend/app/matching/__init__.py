"""Matching a posting against the resume: the prefilter, then the scorer.

Two modules, in the order #9's tick uses them: `prefilter` decides what is worth
scoring without a model, and `scorer` holds the `MatchScorer` interface, the fake
the suite scores with and the real scorer. `LlmScorer` is the one place this app
talks to a model -- one `POST` per posting to the provider's Chat Completions
endpoint over `httpx2`, the prompt in `matching/score_prompt.txt`, the key in
`LLM_API_KEY` -- and every failure it can meet comes back as a `MatchResult` with
a score of 0 and a reason beginning "Not scored: ", never as a traceback.
"""
