# Permissions

What an agent may do in this repository, one row per rule the repository already
states — at the level this repository enforces. "Checked by" names the file that
enforces the rule, or `policy, not checked` with the reason: a rule a file in the
tree cannot test. The build's AI narrative is [`README.md`](../README.md); this is
the enforcement table beside it.

| Rule | Stated in | Checked by |
| --- | --- | --- |
| Never commit a resume, an API key, a database file, or anything from `.env`. | `AGENTS.md` | `backend/tests/test_committed_data_guard.py`; the `.github/workflows/ci.yml` step "Fail on a committed secret, resume or database file"; `agent-hooks/pre-commit` |
| Resume text is user data: never logged, never in an error, never in a response. | `AGENTS.md`, `_docs/process.md` | `backend/tests/test_pipeline.py::test_the_resume_text_reaches_no_log_line_no_row_and_no_response` and its two neighbours, `backend/tests/test_digest.py::test_the_resume_marker_is_in_no_subject_no_body_and_no_log_line` and `backend/tests/test_matches_api.py::test_no_secret_no_resume_text_and_no_database_url_reach_a_body_or_a_log` |
| Secrets live in `.env`, which is gitignored; `.env.example` lists the keys. | `.gitignore` | `backend/tests/test_committed_data_guard.py` |
| The repository is public, so anything committed is world-readable. | `_docs/plan.md` | `policy, not checked` — visibility is a GitHub setting, not a file in the tree; the first row's guard enforces its consequence |
| QA changes no code; the engineer commits and does not close the issue. | `_docs/team/qa-engineer.md`, `_docs/team/software-engineer.md` | `policy, not checked` — a prompt a session follows, not a rule the tree can test |
| One writer per worktree. | No file in this repository — the orchestrating session's rule | `policy, not checked` — recorded here for completeness; nothing in the tree states or checks it |

Criterion 13's AI tool and data policy and its agent/extension security notes are
#82. When that lands it links from here rather than repeating these rows.
