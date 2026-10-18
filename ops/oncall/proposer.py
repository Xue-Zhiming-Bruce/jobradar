"""On-call proposer: draft a code fix for an incident the responder could not fix, and
hand it to a human as a draft pull request.

This is the third response of the on-call agent (#96), beyond the bounded two-action
responder (#93). `responder.py` reads one `alert` issue, takes at most one bounded
action -- a same-origin `POST /internal/tick`, or `ops/rollback.sh` -- and answers on
the issue. When its own answer does not say the incident is fixed, the incident is one
that retrigger and rollback could not remediate, and this module proposes an
application code fix for it.

**The incident it proposes for (AC1).** The diagnosis is the responder's own answer on
the alert issue: it carries what the responder found, what it decided and why, and the
alert body it read. This module refuses a closed issue, an issue the responder has not
answered, an answer that says the incident is fixed (`- **Fixed:** yes`), an answer
whose fixed line it cannot read, and an issue it has already answered once. One
incident, one proposal attempt: the comment it writes carries `PROPOSAL_MARKER`, so the
`opened` and `labeled` deliveries of one alert cannot push two branches or open two
pull requests.

**The proposal (AC1).** It asks the model which application files under `backend/app/`
the incident implicates, hands it those files' own contents at `HEAD`, and applies the
one git unified diff it answers on a new branch. A patch is applied only when it names
at least one file, names nothing outside the files the model was handed, renames
nothing and is bounded, so a patch the model aimed at the review gate's own workflow is
refused before `git apply` runs at all.

**The human review gate (AC2).** It pushes that branch alone -- `oncall/proposal-<issue
number>`, built in `proposal_branch` and nowhere else -- and opens it as a **draft pull
request against `main`**, with the repository owner's review requested, and comments
the link on the alert issue. No path here pushes `main`, merges, approves, force-pushes
or deploys, and `ops/oncall/test_proposer.py` reads back every command this path can
issue to pin that. A draft cannot be merged until a human marks it ready, and
`deploy.yml` deploys only a push to `main`, which only a human merge makes. Requiring
the review itself is a repository setting this file cannot see -- Settings > Branches >
require a pull request before merging, with at least one approving review on `main` --
recorded here because `deploy.yml` records Render's autodeploy setting the same way.

**What it never carries (AGENTS.md).** The two prompts and the pull request body carry
the responder's answer and the alert body, both restricted to ids, statuses and route
names by #92 and #93, and passed through `responder.redact` again here, so a credential
or a connection string cannot reach a prompt, the pull request or the one comment even
if it reached the evidence.

**The seams.** The model is `responder.llm_complete`, and every `git` and `gh` command
-- the file listing, a file's contents, the branch, the patch, the commit, the push,
the pull request and the comment -- goes through one `command` seam. That is what lets
the offline test drive the whole path over fakes, and read back the exact commands the
path issued, which is what AC3's pin needs: a typed seam per operation would hide the
one place a push to `main` could be written.

The standard library and `responder` are the whole dependency.
"""

import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# `ops/oncall` is not a package: its modules are loaded by path -- the way the offline
# tests load them -- so this file puts its own directory on `sys.path` before importing
# its neighbour, and works both as `python3 ops/oncall/proposer.py` and loaded by path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import responder  # noqa: E402 -- the import above must run first

Complete = Callable[[str], str | None]

# The branch every proposal is pushed to and opened from, and the one place the name is
# built: "never `main`" is structural because the branch is this prefix plus the issue
# number, and no other code writes a ref this module pushes.
PROPOSAL_BRANCH_PREFIX = "oncall/proposal-"

# The branch a proposal is opened against -- the default branch, never a proposal's own.
BASE_BRANCH = "main"

# The marker the one comment carries, so one incident is one proposal attempt.
PROPOSAL_MARKER = "<!-- jobradar-oncall-proposal -->"

# The line `responder.comment_body` writes for the issue, and the only three values it
# can hold. `no` is a remediation that did not work and `not verified` is an escalation;
# both are incidents this path proposes for. `yes` is the responder's verified fix, and
# a comment carrying no such line is not an answer this module can read.
FIXED_LINE = re.compile(r"^- \*\*Fixed:\*\* (yes|no|not verified)\s*$", re.MULTILINE)

# The only paths a proposal may name or touch: the application's own code. `.github/`,
# `ops/` and the workflows are deliberately outside it, so a proposal cannot edit the
# gate that reviews it.
APPLICATION_PREFIX = "backend/app/"

# The file headers a git unified diff writes. Every path a patch names is read from
# these and checked against the files the model was handed.
DIFF_PATHS = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)

# The pull request's own address, which `gh pr create` prints on its last line.
PULL_REQUEST_URL = re.compile(r"https://\S+")

# How much of the incident's evidence a prompt and the pull request body quote, how much
# of one file the model is handed, how many files one proposal may touch, and how long
# its diff may be. The model's context is bounded by the first three.
EVIDENCE_LIMIT = 4000
CONTENT_LIMIT = 12000
FILES_LIMIT = 4
DIFF_LIMIT = 200000

# The title's tail, so one long model answer cannot become the pull request's title.
TITLE_LIMIT = 120

# The committer the one commit is made as, so the pull request shows a bot author and
# no personal identity. A GitHub checkout has no committer configured, so it is named
# on the command rather than only in the environment.
COMMITTER_NAME = "github-actions[bot]"
COMMITTER_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
GIT_IDENTITY = ["-c", f"user.name={COMMITTER_NAME}", "-c", f"user.email={COMMITTER_EMAIL}"]

# One git or gh command is bounded; a command that stalls is a failure this module
# reports, never a wait without end.
COMMAND_TIMEOUT = 300


@dataclass(frozen=True)
class Ran:
    """One `git` or `gh` command's answer: its exit code and its two streams.

    The streams are kept apart because a command's stdout is read as data -- `gh pr
    create`'s pull request URL, `git show`'s file contents, `git ls-files`' listing --
    and its stderr is only ever quoted in a failure.
    """

    code: int
    out: str = ""
    err: str = ""

    @property
    def output(self) -> str:
        """Both streams as one text, which is what a reported failure quotes."""
        return f"{self.out}{self.err}"


# The one seam every command goes through: the argv, the stdin a patch or a body is
# written through, and the answer. The offline test replaces it.
Command = Callable[[Sequence[str], str | None], Ran]


@dataclass(frozen=True)
class Diagnosis:
    """The diagnosed incident, and the evidence this proposal reasons from (AC1)."""

    number: int
    evidence: str


@dataclass(frozen=True)
class Proposal:
    """What one wake proposed: the branch, the draft pull request and the model's reason."""

    branch: str
    url: str
    summary: str


@dataclass(frozen=True)
class Outcome:
    """What one wake answered with: the pull request it opened, or why it opened none.

    Both fields empty is a wake with nothing to propose, which is a no-op and not a
    failure -- the incident is fixed, unanswered, or already proposed over.
    """

    proposal: Proposal | None = None
    failure: str | None = None


@dataclass(frozen=True)
class Config:
    """The run's inputs: the login whose review is requested, and the values to redact."""

    reviewer: str
    secrets: tuple[str, ...] = ()


class ProposalFailed(RuntimeError):
    """A step of the proposal path failed, with the sentence the issue comment carries."""


# --- The incident and the one-proposal rule (AC1) ----------------------------


def proposal_branch(issue_number: int) -> str:
    """The one branch a proposal for this issue is pushed to and opened from."""
    return f"{PROPOSAL_BRANCH_PREFIX}{issue_number}"


def responder_answer(comments: Sequence[str]) -> str | None:
    """The responder's own comment on the alert issue, or `None` when it wrote none."""
    for body in reversed(comments):
        if responder.COMMENT_MARKER in body:
            return body
    return None


def read_diagnosis(issue: responder.Issue) -> Diagnosis | None:
    """The unfixed incident to propose for, or `None` when there is nothing to propose.

    `None` for a closed issue (the responder closes the one it verified, and a human
    closing one is a decision too), for an issue this module has already answered once
    -- the one-proposal rule -- for an issue the responder has not answered yet, for an
    answer that says the incident is fixed, and for an answer whose fixed line this
    module cannot read.
    """
    if issue.state == "closed":
        return None
    if any(PROPOSAL_MARKER in body for body in issue.comments):
        return None
    answer = responder_answer(issue.comments)
    if answer is None:
        return None
    fixed = FIXED_LINE.search(answer)
    if fixed is None or fixed.group(1) == "yes":
        return None
    return Diagnosis(number=issue.number, evidence=_evidence(issue.body, answer))


def _evidence(body: str, answer: str) -> str:
    """The incident's evidence: the responder's answer and the alert body, bounded."""
    evidence = (
        f"The responder's own answer on the issue:\n\n{answer.strip()}\n\n"
        f"---\n\nThe alert issue's own body:\n\n{body.strip()}"
    )
    if len(evidence) > EVIDENCE_LIMIT:
        return evidence[:EVIDENCE_LIMIT] + "\n[the rest of the evidence is not shown]"
    return evidence


# --- The model's two answers (AC1) -------------------------------------------


def application_files(command: Command) -> list[str]:
    """The application's own source files, as `git` lists them at HEAD.

    A listing that does not answer is an empty list: the wake reports that it had no
    file to propose over rather than guess at the repository's contents.
    """
    ran = command(["git", "ls-files", APPLICATION_PREFIX], None)
    if ran.code != 0:
        return []
    return sorted(path.strip() for path in ran.out.splitlines() if path.strip())


def select_prompt(evidence: str, files: Sequence[str]) -> str:
    """The first model call: which application files the incident implicates (AC1)."""
    return (
        "You are JobRadar's on-call engineer. An alert was answered by the bounded "
        "responder, which could not fix the incident; its own answer is the evidence "
        "below, followed by the application's source files.\n"
        f"Name the files a fix would have to change -- at most {FILES_LIMIT} paths, and "
        "only paths from that list.\n"
        'Answer with one JSON object and nothing else: {"files": ["<path>", ...]}\n\n'
        f"Evidence:\n{evidence}\n\n"
        "The application's source files:\n" + "\n".join(files)
    )


def parse_selection(raw: str, allowed: Sequence[str]) -> list[str]:
    """The paths a model selected, keeping only ones it was handed, bounded and in order."""
    payload = responder.parse_json_object(raw)
    if payload is None:
        return []
    selected = payload.get("files")
    if not isinstance(selected, list):
        return []
    paths: list[str] = []
    for path in selected:
        if isinstance(path, str) and path in allowed and path not in paths:
            paths.append(path)
    return paths[:FILES_LIMIT]


def read_files(command: Command, paths: Sequence[str]) -> list[tuple[str, str]]:
    """The selected files' own contents at HEAD, each bounded.

    Read from `HEAD` rather than the working tree, so the patch is written against the
    revision the proposal's branch is created from.
    """
    contents: list[tuple[str, str]] = []
    for path in paths:
        ran = command(["git", "show", f"HEAD:{path}"], None)
        if ran.code != 0:
            raise ProposalFailed(
                f"`git show HEAD:{path}` failed: {responder.last_lines(ran.output)}"
            )
        text = ran.out
        if len(text) > CONTENT_LIMIT:
            text = text[:CONTENT_LIMIT] + "\n[the rest of this file is not shown]"
        contents.append((path, text))
    return contents


def patch_prompt(evidence: str, contents: Sequence[tuple[str, str]]) -> str:
    """The second model call: the incident, and the contents of the files it named (AC1)."""
    files = "\n\n".join(f"=== {path} ===\n{text}" for path, text in contents)
    return (
        "You are JobRadar's on-call engineer. The incident below is one the bounded "
        "responder could not fix, and the current contents of the application files you "
        "named follow it.\n"
        "Write the smallest fix as one git unified diff against exactly those files: "
        "`diff --git a/<path> b/<path>` headers, hunks with exact context lines, no "
        "renamed, new or deleted files, and no change outside them. A human reviews this "
        "pull request, so keep it minimal and in the style the file already uses.\n"
        'Answer with one JSON object and nothing else: {"summary": "<one short line>", '
        '"diff": "<the unified diff>"}\n\n'
        f"Evidence:\n{evidence}\n\n{files}"
    )


def parse_patch(raw: str, allowed: Sequence[str]) -> tuple[str, str] | None:
    """The `summary` and `diff` of a model's answer, or `None` when it is not a patch.

    A patch is usable only when it names at least one file, names nothing outside the
    files the model was handed, renames nothing and is bounded -- so a patch aimed at
    `.github/workflows/`, at another file whose contents the model never saw, or at
    anything outside `backend/app/` is refused before `git apply` runs at all.

    shortcut: git quotes a path that holds a space, and a quoted header does not match,
    so such a file cannot be proposed; upgrade if an application file ever gains one.
    """
    payload = responder.parse_json_object(raw)
    if payload is None:
        return None
    summary, diff = payload.get("summary"), payload.get("diff")
    if not isinstance(summary, str) or not isinstance(diff, str):
        return None
    if not diff.strip() or len(diff) > DIFF_LIMIT:
        return None
    paths = DIFF_PATHS.findall(diff)
    if not paths:
        return None
    for before, after in paths:
        if before != after or before not in allowed:
            return None
    return summary.strip(), diff


# --- The proposal, and the one comment (AC1, AC2) -----------------------------


def propose(
    issue: responder.Issue,
    *,
    complete: Complete,
    command: Command,
    config: Config,
) -> Outcome:
    """Draft one code fix for one diagnosed incident as a draft pull request.

    Answers the `Proposal` it opened, `Outcome(failure=...)` when a step of the proposal
    path failed, or an empty `Outcome` when there was nothing to propose. Every failure
    -- a model that answers nothing, a patch it may not apply, a command that refuses,
    a seam that raises -- becomes the one comment naming it, never an exception out of
    the wake; the comment goes through `command` outside that wrapper, so one path
    cannot write two. A comment that cannot be written is the one failure this module
    cannot report on the issue, so it ends the wake red, exactly as the responder's own
    `gh` comment does.
    """
    diagnosis = read_diagnosis(issue)
    if diagnosis is None:
        return Outcome()
    try:
        proposal = _propose(diagnosis, complete=complete, command=command, config=config)
    except ProposalFailed as failure:
        outcome = Outcome(failure=responder.redact(str(failure), config.secrets))
    except Exception as error:  # a seam that raises is a failure to report, not a crash
        outcome = Outcome(
            failure=responder.redact(
                f"the proposal path could not complete: {type(error).__name__}: {error}",
                config.secrets,
            )
        )
    else:
        outcome = Outcome(proposal=proposal)

    commented = command(
        ["gh", "issue", "comment", str(issue.number), "--body-file", "-"],
        comment_body(outcome),
    )
    if commented.code != 0:
        raise RuntimeError(
            responder.redact(
                "the issue comment failed: " + responder.last_lines(commented.output),
                config.secrets,
            )
        )
    return outcome


def _propose(
    diagnosis: Diagnosis, *, complete: Complete, command: Command, config: Config
) -> Proposal:
    """Ask the model twice, apply its patch on the proposal's branch, and open the draft PR."""
    evidence = responder.redact(diagnosis.evidence, config.secrets)
    files = application_files(command)
    if not files:
        raise ProposalFailed(
            f"`git ls-files {APPLICATION_PREFIX}` answered no application file, so there "
            "is nothing to propose over"
        )

    raw_selection = complete(select_prompt(evidence, files))
    if raw_selection is None:
        raise ProposalFailed("the model did not answer, so no patch could be drafted")
    selected = parse_selection(raw_selection, files)
    if not selected:
        raise ProposalFailed("the model's answer named no application file to change")

    raw_patch = complete(patch_prompt(evidence, read_files(command, selected)))
    if raw_patch is None:
        raise ProposalFailed("the model did not answer the request for a patch")
    patch = parse_patch(raw_patch, selected)
    if patch is None:
        raise ProposalFailed(
            "the model's answer was not a patch over the files it was handed, so it was "
            "not applied"
        )
    summary, diff = patch
    summary = responder.redact(summary, config.secrets)

    branch = proposal_branch(diagnosis.number)
    steps = (
        ("creating the branch", ["git", "checkout", "-b", branch], None),
        ("applying the patch", ["git", "apply", "--whitespace=nowarn", "-"], diff),
        (
            "committing the patch",
            ["git", *GIT_IDENTITY, "commit", "-a", "-m", commit_message(diagnosis, summary)],
            None,
        ),
        ("pushing the branch", ["git", "push", "origin", f"HEAD:refs/heads/{branch}"], None),
    )
    for what, argv, stdin in steps:
        ran = command(argv, stdin)
        if ran.code != 0:
            raise ProposalFailed(
                f"{what} failed: {responder.redact(responder.last_lines(ran.output), config.secrets)}"
            )

    opened = command(
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--base",
            BASE_BRANCH,
            "--head",
            branch,
            "--title",
            pr_title(diagnosis, summary),
            "--body-file",
            "-",
            "--reviewer",
            config.reviewer,
        ],
        proposal_body(diagnosis, summary, evidence),
    )
    if opened.code != 0:
        raise ProposalFailed(
            "opening the draft pull request failed: "
            + responder.redact(responder.last_lines(opened.output), config.secrets)
        )
    url = _pull_request_url(opened.out)
    if url is None:
        raise ProposalFailed("`gh pr create` answered no pull request URL")
    return Proposal(branch=branch, url=url, summary=summary)


def _pull_request_url(output: str) -> str | None:
    """The pull request's own URL, which `gh pr create` prints as its last line."""
    urls = PULL_REQUEST_URL.findall(output)
    return urls[-1] if urls else None


def commit_message(diagnosis: Diagnosis, summary: str) -> str:
    """The one commit's message: the incident it answers, and what the patch does."""
    return f"On-call proposal for #{diagnosis.number}: {summary or 'proposed application fix'}"


def pr_title(diagnosis: Diagnosis, summary: str) -> str:
    """The pull request's title: the incident's issue number and the model's short reason."""
    tail = summary.strip().splitlines()[0][:TITLE_LIMIT] if summary.strip() else ""
    return f"On-call proposal for #{diagnosis.number}: {tail or 'proposed application fix'}"


def proposal_body(diagnosis: Diagnosis, summary: str, evidence: str) -> str:
    """The pull request's body: the evidence it reasoned from, and the review it needs (AC1, AC2)."""
    return "\n".join(
        [
            f"## On-call proposal for #{diagnosis.number}",
            "",
            "**Draft: this needs a human review before it can merge.** A human reviews and",
            "merges this; nothing in the on-call agent pushes `main`, merges, approves or",
            "deploys.",
            "",
            f"**What the patch does:** {summary or 'the model named no reason'}",
            "",
            "### The evidence this reasoned from",
            "",
            evidence,
            "",
            f"Opened by `ops/oncall/proposer.py` (#96) on branch "
            f"`{proposal_branch(diagnosis.number)}` from the responder's own answer on the",
            "alert issue.",
        ]
    )


def comment_body(outcome: Outcome) -> str:
    """The one comment a wake writes on the alert issue: the pull request, or why none.

    Only ids, statuses, the branch, the pull request's URL, the redacted summary and the
    redacted failure reach it, so no resume, file name or credential can (AGENTS.md).
    """
    if outcome.proposal is None:
        return "\n".join(
            [
                PROPOSAL_MARKER,
                "## On-call proposal: none",
                "",
                f"- **No patch was proposed:** {outcome.failure}",
                "- **Proposed, merged or deployed:** nothing.",
            ]
        )
    return "\n".join(
        [
            PROPOSAL_MARKER,
            "## On-call proposal opened",
            "",
            f"- **Branch:** `{outcome.proposal.branch}`",
            f"- **Draft pull request:** {outcome.proposal.url}",
            f"- **What it proposes:** {outcome.proposal.summary or 'the model named no reason'}",
            "- **Merged, approved or deployed:** no -- a human reviews and merges it.",
        ]
    )


# --- The real seam and the wake ------------------------------------------------


def subprocess_command(argv: Sequence[str], stdin: str | None = None) -> Ran:
    """The real command seam: one `git` or `gh` command, its exit code and its two streams.

    A command that cannot be run, or does not finish inside the bound, is a failure this
    module reports, never an exception out of the wake. No credential is ever an
    argument: `gh` reads its token from the environment and every body is written
    through stdin.
    """
    try:
        completed = subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return Ran(1, err=f"{' '.join(argv[:2])} did not finish within {COMMAND_TIMEOUT}s")
    except OSError as error:
        return Ran(1, err=f"{argv[0]} could not be run: {type(error).__name__}")
    return Ran(completed.returncode, completed.stdout, completed.stderr)


def main() -> int:
    """Read the environment, propose once, and print a redacted one-line outcome.

    The three values a proposal needs are the ones `oncall.yml`'s guard step has already
    checked; this refuses them again rather than drafting a patch with no model or
    opening a pull request with nobody asked to review it. No wake reads a resolved
    value into its output.
    """
    number_text = os.environ.get("ISSUE_NUMBER", "").strip()
    if not number_text.isdigit():
        print(
            "proposer: ISSUE_NUMBER is unset, empty or not a number, so there is no alert "
            "issue to answer.",
            file=sys.stderr,
        )
        return 2
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        print("proposer: LLM_API_KEY is unset or empty, so no patch can be drafted.", file=sys.stderr)
        return 2
    reviewer = os.environ.get("PROPOSAL_REVIEWER", "").strip()
    if not reviewer:
        print(
            "proposer: PROPOSAL_REVIEWER is unset or empty, so no review can be requested.",
            file=sys.stderr,
        )
        return 2

    secrets = responder.secrets_from_environment()
    config = Config(reviewer=reviewer, secrets=secrets)
    complete = responder.llm_complete(
        api_key,
        os.environ.get("LLM_MODEL", "").strip() or responder.DEFAULT_MODEL,
        os.environ.get("LLM_BASE_URL", "").strip() or responder.DEFAULT_MODEL_BASE_URL,
    )

    try:
        issue = responder.gh_issue(int(number_text))
    except Exception as error:  # the issue cannot be read, so nothing is diagnosed
        print(
            responder.redact(
                f"proposer: could not read the issue: {type(error).__name__}", secrets
            ),
            file=sys.stderr,
        )
        return 1

    outcome = propose(issue, complete=complete, command=subprocess_command, config=config)
    if outcome.proposal is not None:
        print(
            responder.redact(
                f"proposer: opened a draft pull request on {outcome.proposal.branch}: "
                f"{outcome.proposal.url}",
                secrets,
            )
        )
        return 0
    if outcome.failure is not None:
        print(
            responder.redact(f"proposer: no proposal: {outcome.failure}", secrets),
            file=sys.stderr,
        )
        return 1
    print(
        "proposer: nothing to propose; the incident is fixed, unanswered, or already "
        "proposed over."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
