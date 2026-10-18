"""`ops/oncall/proposer.py`'s proposal path, driven offline over a fake model and fake commands.

#96's AC3: the proposal path is exercised against fakes -- no model, no `git`, no `gh`
and no repository -- so the branch it would push, the patch it would apply, the draft
pull request it would open and the one comment it writes are facts rather than hopes.
The cases cover the incident it proposes for (an escalation and a retrigger that failed
again) and the ones it declines (a verified fix, an unanswered issue, a closed issue, an
incident it has already proposed over), a patch refused for naming a file the model was
never handed, a diff that does not apply, a model that answers nothing and a seam that
raises. AC2's pin is `test_no_path_...`: every command the path can issue, on each of
those branches, is read back and checked to be one that pushes `main`, merges, approves
or deploys nothing.

The fixture the diagnosis is read from is built with `responder.comment_body` rather than
typed out, so the field this path reads (`- **Fixed:**`) cannot drift from the comment
the responder really writes.

The module lives under `ops/`, outside the backend package, so it is loaded by path the
way `ops/oncall/test_responder.py` loads `responder.py`: it is the real file, not a copy
that could drift.
"""

import importlib.util
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODULE_PATH = REPO_ROOT / "ops" / "oncall" / "proposer.py"

_spec = importlib.util.spec_from_file_location("proposer", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
proposer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proposer)

responder = proposer.responder

ISSUE_NUMBER = 97
REVIEWER = "reviewer-login"
BRANCH = f"oncall/proposal-{ISSUE_NUMBER}"
PR_URL = "https://github.com/example/jobradar/pull/97"

APP_FILE = "backend/app/routes/internal.py"
OTHER_FILE = "backend/app/telemetry.py"
APP_LISTING = "\n".join([APP_FILE, OTHER_FILE, "backend/app/main.py"]) + "\n"
FILE_TEXT = "def tick():\n    return 202\n"

# The commit is the one command that carries the bot's identity before the verb, so the
# table names it by that identity rather than by `git commit`.
COMMIT = f"git -c user.name={proposer.COMMITTER_NAME}"

# The alert body #92's `Report.body` writes, and the reason #93's responder records.
ALERT_BODY = "\n".join(
    [
        "## JobRadar digest journey",
        "",
        "User impact detected: `newest_run_failed`.",
        "",
        "- id `7`, status `failed`",
    ]
)
RESPONDER_REASON = "the alert's run `7` ended `failed`, so re-run the digest"

# The two answers the fake model gives: which files, then the patch over their contents.
SELECTION = json.dumps({"files": [APP_FILE]})
DIFF = "\n".join(
    [
        f"diff --git a/{APP_FILE} b/{APP_FILE}",
        f"--- a/{APP_FILE}",
        f"+++ b/{APP_FILE}",
        "@@ -1,2 +1,2 @@",
        " def tick():",
        "-    return 202",
        "+    return 202 if ready() else 503",
        "",
    ]
)
PATCH = json.dumps({"summary": "answer 503 while the app is not ready", "diff": DIFF})

# A planted credential and a planted connection string, assembled from parts so this
# module's own source carries neither a secret-shaped literal nor a URL with a password
# (the rule `backend/tests/test_committed_data_guard.py` enforces).
SECRET = "oncall-" + "sentinel-" + "not-a-real-key"
CONNECTION_STRING = "postgresql://jobradar:" + "planted" + "@db.example.test:5432/jobradar"


def ran(code: int = 0, out: str = "", err: str = "") -> proposer.Ran:
    """One command's answer, with the names this file reads it by."""
    return proposer.Ran(code, out, err)


def answers(overrides: dict[str, proposer.Ran] | None = None) -> dict[str, proposer.Ran]:
    """The prefix table a run of the proposal path is answered from.

    A key is the start of a command's own text, so a test overrides the one command it
    means to fail without repeating the argv the proposer builds.
    """
    table = {
        "git ls-files": ran(out=APP_LISTING),
        "git show HEAD:": ran(out=FILE_TEXT),
        "git checkout -b": ran(out=f"Switched to a new branch '{BRANCH}'\n"),
        "git apply": ran(),
        COMMIT: ran(out="[oncall/proposal-97 abc1234] On-call proposal\n"),        "git push": ran(out=f"To https://github.com/example/jobradar.git\n * [new branch] {BRANCH}\n"),
        "gh pr create": ran(out=PR_URL + "\n"),
        "gh issue comment": ran(out="https://github.com/example/jobradar/issues/97#issuecomment-1\n"),
    }
    table.update(overrides or {})
    return table


class FakeCommands:
    """A `command` seam that answers from a prefix table and records every command.

    A command no key matches is a test written wrong, so it fails by name instead of
    inventing an answer, and `calls` is what the guard case reads to see the whole
    command vocabulary of every branch of the path.
    """

    def __init__(self, table: dict[str, proposer.Ran]) -> None:
        self._table = table
        self.calls: list[tuple[str, str | None]] = []

    def _label(self, line: str) -> str:
        for prefix in sorted(self._table, key=len, reverse=True):
            if line.startswith(prefix):
                return prefix
        raise AssertionError(f"unexpected command: {line}")

    def __call__(self, argv: Sequence[str], stdin: str | None = None) -> proposer.Ran:
        line = " ".join(argv)
        label = self._label(line)
        self.calls.append((line, stdin))
        return self._table[label]

    def labels(self) -> list[str]:
        """The table key each recorded command matched, in the order they were issued."""
        return [self._label(line) for line, _ in self.calls]

    def issued(self, prefix: str) -> list[str]:
        """Every recorded command whose own text starts with `prefix`."""
        return [line for line, _ in self.calls if line.startswith(prefix)]

    def stdin_for(self, prefix: str) -> str:
        """The stdin the one recorded command matching `prefix` was given."""
        matched = [(line, stdin) for line, stdin in self.calls if line.startswith(prefix)]
        assert len(matched) == 1, f"{len(matched)} commands matched {prefix!r}"
        assert matched[0][1] is not None
        return matched[0][1]


def responder_answer(fixed: bool | None) -> str:
    """The comment `responder.comment_body` writes for one outcome -- the diagnosis read."""
    return responder.comment_body(
        responder.Outcome(
            action=responder.ESCALATE if fixed is None else responder.RETRIGGER,
            source="deterministic",
            reason=RESPONDER_REASON,
            found="`GET /healthz` -> HTTP 200; `GET /runs/7` -> status `failed`",
            performed=None if fixed is None else "retriggered the digest",
            run_id=7,
            trace_id=None,
            fixed=fixed,
            owner_action=None if fixed is not None else "a human must investigate the alert.",
        )
    )


def propose_offline(
    comments: Sequence[str] = (),
    *,
    state: str = "open",
    body: str = ALERT_BODY,
    model_replies: Sequence[str | None] = (SELECTION, PATCH),
    commands: Any = None,
    secrets: tuple[str, ...] = (),
) -> tuple[proposer.Outcome, Any, list[str]]:
    """Drive one wake of the proposal path offline, and answer what it did and asked.

    `comments` defaults to no comment at all -- an issue the responder has not answered
    -- so every case that means to propose has to say which answer it is proposing over.
    """
    seam = commands if commands is not None else FakeCommands(answers())
    prompts: list[str] = []
    replies = list(model_replies)

    def complete(prompt: str) -> str | None:
        prompts.append(prompt)
        return replies.pop(0) if replies else None

    outcome = proposer.propose(
        responder.Issue(ISSUE_NUMBER, state, body, tuple(comments)),
        complete=complete,
        command=seam,
        config=proposer.Config(reviewer=REVIEWER, secrets=secrets),
    )
    return outcome, seam, prompts


# AC1: the proposed change, and the evidence it reasoned from.


def test_an_unfixed_incident_is_proposed_as_a_draft_pull_request() -> None:
    outcome, commands, prompts = propose_offline((responder_answer(False),))

    assert outcome.failure is None
    assert outcome.proposal is not None
    assert outcome.proposal.branch == BRANCH
    assert outcome.proposal.url == PR_URL
    # The whole path, in order: list the files, read the one the model named, branch,
    # apply, commit, push that branch alone, open the draft, comment once.
    assert commands.labels() == [
        "git ls-files",
        "git show HEAD:",
        "git checkout -b",
        "git apply",
        COMMIT,
        "git push",
        "gh pr create",
        "gh issue comment",
    ]
    assert commands.issued("git show HEAD:") == [f"git show HEAD:{APP_FILE}"]
    # The one commit is made as the bot, so the pull request shows no personal identity.
    assert f"user.email={proposer.COMMITTER_EMAIL}" in commands.issued(COMMIT)[0]
    assert commands.issued(f"git push origin HEAD:refs/heads/{BRANCH}") == [
        f"git push origin HEAD:refs/heads/{BRANCH}"
    ]
    assert commands.stdin_for("git apply") == DIFF
    opened = commands.issued("gh pr create")[0]
    assert "--draft" in opened
    assert f"--base {proposer.BASE_BRANCH}" in opened
    assert f"--head {BRANCH}" in opened
    assert f"--reviewer {REVIEWER}" in opened
    # One comment on the alert issue, carrying the pull request it opened.
    comment = commands.stdin_for("gh issue comment")
    assert proposer.PROPOSAL_MARKER in comment
    assert PR_URL in comment
    assert BRANCH in comment
    # The model was asked twice: which files, then for a patch over what they hold.
    assert len(prompts) == 2
    assert APP_FILE in prompts[0] and OTHER_FILE in prompts[0]
    assert FILE_TEXT.strip() in prompts[1]


def test_the_pull_request_body_carries_the_evidence_it_reasoned_from() -> None:
    _, commands, _ = propose_offline((responder_answer(False),))

    body = commands.stdin_for("gh pr create")

    assert RESPONDER_REASON in body
    assert "- id `7`, status `failed`" in body
    assert "needs a human review" in body
    assert proposer.PROPOSAL_MARKER not in body  # the marker belongs to the issue comment


def test_a_retrigger_that_failed_again_is_proposed() -> None:
    """`Fixed: no` is a remediation that did not work, so the incident needs a code fix."""
    outcome, _, _ = propose_offline((responder_answer(False),))

    assert outcome.proposal is not None


def test_an_escalation_is_proposed() -> None:
    """`Fixed: not verified` is the other incident the bounded action set left unfixed."""
    outcome, _, _ = propose_offline((responder_answer(None),))

    assert outcome.proposal is not None


# AC1: the incidents this path must not propose over.


def test_a_verified_fix_is_not_proposed() -> None:
    outcome, commands, prompts = propose_offline((responder_answer(True),))

    assert outcome.proposal is None and outcome.failure is None
    assert commands.calls == [] and prompts == []


def test_an_issue_the_responder_has_not_answered_is_not_proposed() -> None:
    """Nothing is diagnosed yet, so there is no incident to reason about."""
    outcome, commands, _ = propose_offline(())

    assert outcome.proposal is None and outcome.failure is None
    assert commands.calls == []


def test_an_issue_that_already_has_a_proposal_is_not_proposed() -> None:
    """One incident, one proposal attempt: the marker its own comment writes is the rule."""
    outcome, commands, _ = propose_offline(
        (responder_answer(None), f"an earlier answer\n{proposer.PROPOSAL_MARKER}")
    )

    assert outcome.proposal is None and outcome.failure is None
    assert commands.calls == []


def test_an_answer_the_proposer_cannot_read_is_not_proposed() -> None:
    """A comment that does not carry the responder's fixed line is not an answer to act on."""
    outcome, commands, _ = propose_offline(
        (f"{responder.COMMENT_MARKER}\n## On-call response: escalate",)
    )

    assert outcome.proposal is None and outcome.failure is None
    assert commands.calls == []


def test_a_closed_issue_is_not_proposed() -> None:
    outcome, commands, _ = propose_offline((responder_answer(False),), state="closed")

    assert outcome.proposal is None and outcome.failure is None
    assert commands.calls == []


# AC1, AC2: a patch may touch only the application files the model was handed.


def test_a_patch_that_names_a_file_outside_the_application_is_refused() -> None:
    """A proposal may not reach the review gate's own workflow."""
    outside = json.dumps(
        {
            "summary": "weaken the gate",
            "diff": DIFF.replace(APP_FILE, ".github/workflows/oncall.yml"),
        }
    )

    outcome, commands, _ = propose_offline(
        (responder_answer(False),), model_replies=(SELECTION, outside)
    )

    assert outcome.proposal is None
    assert outcome.failure is not None and "not a patch over the files it was handed" in outcome.failure
    assert commands.issued("git checkout") == []
    assert commands.issued("git push") == []
    assert commands.issued("gh pr create") == []
    assert proposer.PROPOSAL_MARKER in commands.stdin_for("gh issue comment")


def test_a_patch_touching_a_file_the_model_was_not_handed_is_refused() -> None:
    """The listing offered the file, but the model never saw its contents."""
    elsewhere = json.dumps({"summary": "change something else", "diff": DIFF.replace(APP_FILE, OTHER_FILE)})

    outcome, commands, _ = propose_offline(
        (responder_answer(False),), model_replies=(SELECTION, elsewhere)
    )

    assert outcome.proposal is None and outcome.failure is not None
    assert commands.issued("git apply") == [] and commands.issued("gh pr create") == []


def test_an_answer_that_is_not_a_patch_is_refused() -> None:
    for raw in (
        "",
        "the fix is obvious",
        json.dumps({"summary": "no diff here"}),
        json.dumps({"summary": "empty", "diff": ""}),
        json.dumps({"summary": "not a diff", "diff": "just some text"}),
        json.dumps({"summary": "half a diff", "diff": f"--- a/{APP_FILE}\n+++ b/{APP_FILE}\n"}),
    ):
        assert proposer.parse_patch(raw, [APP_FILE]) is None, raw


def test_a_patch_that_renames_a_file_is_refused() -> None:
    renamed = json.dumps(
        {"summary": "move it", "diff": DIFF.replace(f"b/{APP_FILE}", f"b/{OTHER_FILE}")}
    )

    assert proposer.parse_patch(renamed, [APP_FILE, OTHER_FILE]) is None


def test_parse_selection_keeps_only_the_files_the_model_was_handed() -> None:
    raw = json.dumps({"files": ["backend/app/main.py", "../../etc/passwd", APP_FILE, APP_FILE]})

    assert proposer.parse_selection(raw, [APP_FILE, "backend/app/main.py"]) == [
        "backend/app/main.py",
        APP_FILE,
    ]


# AC1: a failure is reported on the issue, and nothing is pushed.


def test_a_diff_that_does_not_apply_is_reported_and_nothing_is_pushed() -> None:
    commands = FakeCommands(answers({"git apply": ran(1, err="error: patch does not apply")}))

    outcome, commands, _ = propose_offline((responder_answer(False),), commands=commands)

    assert outcome.proposal is None
    assert outcome.failure is not None and "applying the patch failed" in outcome.failure
    assert commands.issued("git push") == []
    assert commands.issued("gh pr create") == []
    assert proposer.PROPOSAL_MARKER in commands.stdin_for("gh issue comment")


def test_a_model_that_answers_nothing_is_reported_as_a_failure() -> None:
    outcome, commands, _ = propose_offline((responder_answer(False),), model_replies=(None,))

    assert outcome.proposal is None
    assert outcome.failure is not None and "did not answer" in outcome.failure
    assert commands.issued("git push") == []
    assert proposer.PROPOSAL_MARKER in commands.stdin_for("gh issue comment")


def test_a_command_that_raises_still_gets_one_comment() -> None:
    """No failure inside the proposal path stands in for an answer on the issue."""

    class Boom(RuntimeError):
        pass

    class HalfBroken:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, argv: Sequence[str], stdin: str | None = None) -> proposer.Ran:
            self.calls += 1
            if self.calls == 1:
                raise Boom("the seam raised instead of answering")
            return ran(out="https://github.com/example/jobradar/issues/97#issuecomment-1")

    outcome, seam, _ = propose_offline((responder_answer(False),), commands=HalfBroken())

    assert outcome.proposal is None
    assert outcome.failure is not None and "Boom" in outcome.failure
    assert seam.calls == 2  # the listing that raised, and the one comment


def test_a_comment_that_cannot_be_written_ends_the_wake() -> None:
    """The comment is the answer, so failing to write it cannot be reported on the issue."""
    commands = FakeCommands(answers({"gh issue comment": ran(1, err="gh: not logged in")}))

    with pytest.raises(RuntimeError):
        propose_offline((responder_answer(False),), commands=commands)

    assert commands.issued("gh pr create") != []


# AC2, AC3: the pin over every branch of the path.


def test_no_path_pushes_main_merges_approves_or_deploys() -> None:
    """Read back the commands every branch of the path issues: none of them lands a commit."""
    issued: list[str] = []
    for table in (
        answers(),
        answers({"git apply": ran(1, err="error: patch does not apply")}),
        answers({"git push": ran(1, err="! [remote rejected] (protected branch hook declined)")}),
        answers({"gh pr create": ran(1, err="could not create pull request")}),
    ):
        commands = FakeCommands(table)
        propose_offline((responder_answer(False),), commands=commands)
        issued += [line for line, _ in commands.calls]

    # One push, of the proposal's own branch, with no force and no base branch: the
    # three branches that reach it push the same ref and nothing else.
    pushes = [line for line in issued if line.startswith("git push")]
    assert pushes and set(pushes) == {f"git push origin HEAD:refs/heads/{BRANCH}"}
    for line in issued:
        assert "merge" not in line
        assert "deploy" not in line
        assert "--force" not in line
        assert "gh pr review" not in line
        assert "--approve" not in line
        assert "gh workflow run" not in line
        # The one command that names the base branch only opens the pull request over it.
        if f"--base {proposer.BASE_BRANCH}" in line:
            assert line.startswith("gh pr create")


def test_the_proposal_branch_is_never_the_base_branch() -> None:
    assert proposer.BASE_BRANCH == "main"
    for number in (0, 1, 96, 1000, 999999):
        branch = proposer.proposal_branch(number)
        assert branch.startswith(proposer.PROPOSAL_BRANCH_PREFIX)
        assert branch != proposer.BASE_BRANCH
        assert proposer.BASE_BRANCH not in branch


# AGENTS.md: nothing private rides into a prompt, the pull request or the comment.


def test_a_secret_in_the_evidence_and_the_model_summary_reaches_none_of_them() -> None:
    answer = responder_answer(False).replace(RESPONDER_REASON, f"the trace quoted {SECRET}")
    leaky = json.dumps({"summary": f"the key is {SECRET}", "diff": DIFF})

    outcome, commands, prompts = propose_offline(
        (answer,),
        model_replies=(SELECTION, leaky),
        secrets=(SECRET,),
    )

    body = commands.stdin_for("gh pr create")
    comment = commands.stdin_for("gh issue comment")
    assert outcome.proposal is not None
    for text in (*prompts, body, comment):
        assert SECRET not in text
        assert "[redacted]" in body and "[redacted]" in comment


# The wake's own environment guard.


@pytest.mark.parametrize("missing", ["ISSUE_NUMBER", "LLM_API_KEY", "PROPOSAL_REVIEWER"])
def test_main_refuses_an_environment_without_the_values_a_proposal_needs(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv("ISSUE_NUMBER", str(ISSUE_NUMBER))
    monkeypatch.setenv("LLM_API_KEY", "a-key")
    monkeypatch.setenv("PROPOSAL_REVIEWER", REVIEWER)
    monkeypatch.delenv(missing)

    assert proposer.main() == 2


def test_main_refuses_an_issue_number_that_is_not_a_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ISSUE_NUMBER", "not a number")
    monkeypatch.setenv("LLM_API_KEY", "a-key")
    monkeypatch.setenv("PROPOSAL_REVIEWER", REVIEWER)

    assert proposer.main() == 2
