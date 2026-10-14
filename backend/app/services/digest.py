"""The digest: one email per run, carrying every match that qualifies, newest first.

`send_digest(mailer, run_id)` is the whole of it: the matches it
emails are every un-notified match in the database, not the ones this run
created, which is what makes a run that failed mid-walk cost a retry rather than
a missed alert, and the run id is what lets it record the count on the run row it
belongs to. `Mailer` is the interface it sends through, the way #8 declares
`MatchScorer`: `FakeMailer` stands in for the real one so a test asserts exactly
what went out without a host, a port or a credential, and `SmtpMailer` is the
real one -- the only code in this repository that imports `smtplib`. Which mailer
a run sends through is `app.routes.internal.get_mailer()`'s, the one place a
`SmtpMailer` is constructed.

What qualifies is one selection and three rules: `notified_at IS NULL`, a score
at or above `min_score()`, and an `outcome` of `'scored'` -- the row's own marker
of the model having answered it. A rejection and a not-scored result are both
stored with `score = 0`, so at a cutoff of 0 that column is the only thing keeping
a row the scorer never answered out of an email; before #30 the same rule was read
off the reason's text, and the column names the same three states without parsing
it. `state` is deliberately not one of the rules: a match the owner
saved or dismissed before its first email is still emailed, because dismissing
happens while browsing matches and this is the alerting step.

The message text carries, per match, the company's name, the posting's title and
URL, the score, the reason and every gap string #8 stored as JSON -- decoded, one
per line, so no raw JSON is printed. The rows come back newest first, one email
for all of them, never one per match or per company.

`match.notified_at` is the marker and `run.matches_emailed` is the run's own
count, and the one `db.session_scope()` writes both: the marker for exactly the
matches that went out, the count as their number, at one aware UTC instant read
after `send` returned. A selection with nothing in it writes `0` -- this run
emailed nothing -- and no marker at all. A send that raises writes neither: the
exception escapes to #9's `_run`, which records the run failed with the
transport's own message, the count stays NULL ("unknown" is the honest value for
a send that failed), and the same matches are emailed by the next successful run.
Delivery is at-least-once: a database failure between a successful send and this
write re-emails those matches, and the marker write is the only thing that stops
a re-send.

Every variable this module reads is named here and read in no other module, per
call and never at import, the way `app.services.resume.uploads_dir()` reads its
own: `DIGEST_MIN_SCORE` (default 70), `SMTP_HOST`, `SMTP_PORT` (default 587),
`SMTP_USERNAME`, `SMTP_PASSWORD`, `DIGEST_FROM` and `DIGEST_TO`. Real values live
in `.env` (gitignored) and nowhere else -- no host, address, credential or body is
ever logged, stored on a row or committed, and `SmtpMailer` fails the run naming
the missing variable rather than skipping the mail silently.
"""

import json
import os
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Protocol, runtime_checkable

from sqlalchemy import select, update

from app import db
from app.models import Company, Match, Posting, Run

# The seven variables, named here and read in no other module, the way the scorer
# and the prefilter read their own settings in one place.
DIGEST_MIN_SCORE = "DIGEST_MIN_SCORE"
SMTP_HOST = "SMTP_HOST"
SMTP_PORT = "SMTP_PORT"
SMTP_USERNAME = "SMTP_USERNAME"
# The variable's name, not a value: the relay's password is read by that name when a digest sends.
SMTP_PASSWORD = "SMTP_PASSWORD"  # noqa: S105
DIGEST_FROM = "DIGEST_FROM"
DIGEST_TO = "DIGEST_TO"

# What an unset or empty variable means, declared rather than left to a library.
# The cutoff is a score on the same 0-100 scale #8 scores on, so a run whose
# matches all sit below it emails nothing at all.
DEFAULT_MIN_SCORE = 70
DEFAULT_SMTP_PORT = 587

# One connection timeout for the whole conversation with the relay, so a relay
# that accepts the connection and then goes quiet fails the run instead of
# holding #9's run open past an HTTP timeout, the way the scorer's own timeout
# bounds a model call.
SMTP_TIMEOUT_SECONDS = 30.0

# The one subject every digest carries: there is one owner and one recipient, so
# the message needs no per-run label and nothing that could be derived from the
# resume.
DIGEST_SUBJECT = "JobRadar digest"

# What a line of the message begins with. One spelling each, because the body is
# what a reader scans.
_URL_LABEL = "URL: "
_SCORE_LABEL = "Score: "
_REASON_LABEL = "Reason: "
_GAP_LABEL = "Gap: "


class MailerConfigurationError(RuntimeError):
    """The digest was asked to send without something it cannot send without.

    Raised by `SmtpMailer.send` before it opens a connection, so an unconfigured
    deployment fails its run loudly -- with the variable's name, never its value
    -- instead of skipping the mail silently and looking like a run with nothing
    to send.
    """


@dataclass(frozen=True)
class DigestMatch:
    """One match as the message needs it, read while the session is still open.

    A row handed out of `db.session_scope()` is expired by its commit, so the six
    values the body is built from -- and the id the marker is written by -- are
    plain values, read from the match, the posting and the company together.
    """

    id: int
    company: str
    title: str
    url: str
    score: int
    reason: str
    gaps: list[str]


@runtime_checkable
class Mailer(Protocol):
    """Sending one message. The only method the digest knows about."""

    def send(self, *, subject: str, body: str) -> None:
        """Send one message carrying these two strings, or raise.

        The recipient is configuration and not an argument: there is one owner
        and one recipient, so a call cannot address the wrong mailbox.
        """
        ...


class FakeMailer:
    """A `Mailer` that sends nothing: it records every message it was handed.

    Each call appends `(subject, body)` to `sent`, in the order the calls were
    made, so a test asserts what went out -- how many messages, what they carried
    and in which order -- with no host, port, credential or socket in reach.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, *, subject: str, body: str) -> None:
        """Record the message instead of sending it."""
        self.sent.append((subject, body))


class SmtpMailer:
    """The real `Mailer`: one `EmailMessage` over one STARTTLS connection.

    The message is built as an `EmailMessage` and handed to `smtplib`'s
    `send_message`, so the standard library does every encoding there is -- no
    header string is built here, and a body with non-ASCII text in it is encoded
    rather than mangled. Every variable is read per send and never at import, so
    a deployment sets them in the environment and the suite needs none of them
    set: a test that never sends through this class reaches no socket.
    """

    def send(self, *, subject: str, body: str) -> None:
        """Hand one message to the configured relay, or fail naming what is missing.

        `SMTP_HOST`, `DIGEST_FROM` and `DIGEST_TO` are required, and the failure
        names the first one that is unset rather than the value of any of them.
        The username and the password are optional: login happens only when a
        username is set, which is what a local relay without authentication
        needs.
        """
        host = smtp_host()
        sender = digest_from()
        recipient = digest_to()
        for variable, value in (
            (SMTP_HOST, host),
            (DIGEST_FROM, sender),
            (DIGEST_TO, recipient),
        ):
            if not value:
                raise MailerConfigurationError(
                    f"{variable} is not set: sending the digest needs it"
                )
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = recipient
        message.set_content(body)
        with smtplib.SMTP(host, smtp_port(), timeout=SMTP_TIMEOUT_SECONDS) as connection:
            connection.starttls()
            username = smtp_username()
            if username:
                connection.login(username, smtp_password())
            connection.send_message(message)


def min_score() -> int:
    """`DIGEST_MIN_SCORE`, or `DEFAULT_MIN_SCORE` when it is unset or empty.

    Read per call, so a deployment tunes the cutoff by setting the variable and a
    test sets one instead of storing a high-scoring match. `0` is a legitimate
    value: at 0 every genuinely scored match qualifies, and the row's own
    `outcome` is what keeps the rows nobody scored out.
    """
    configured = os.environ.get(DIGEST_MIN_SCORE, "").strip()
    return int(configured) if configured else DEFAULT_MIN_SCORE


def smtp_host() -> str:
    """`SMTP_HOST`, or the empty string when it is unset or empty."""
    return os.environ.get(SMTP_HOST, "").strip()


def smtp_port() -> int:
    """`SMTP_PORT`, or `DEFAULT_SMTP_PORT` when it is unset or empty."""
    configured = os.environ.get(SMTP_PORT, "").strip()
    return int(configured) if configured else DEFAULT_SMTP_PORT


def smtp_username() -> str:
    """`SMTP_USERNAME`, or the empty string when it is unset or empty: no login then."""
    return os.environ.get(SMTP_USERNAME, "").strip()


def smtp_password() -> str:
    """`SMTP_PASSWORD`, or the empty string when it is unset or empty.

    A credential, read per send and handed to `smtplib`'s `login` and to nothing
    else: it is never logged, never stored on a row and never in a message.
    """
    return os.environ.get(SMTP_PASSWORD, "").strip()


def digest_from() -> str:
    """`DIGEST_FROM`, or the empty string when it is unset or empty."""
    return os.environ.get(DIGEST_FROM, "").strip()


def digest_to() -> str:
    """`DIGEST_TO`, or the empty string when it is unset or empty."""
    return os.environ.get(DIGEST_TO, "").strip()


def send_digest(mailer: Mailer, run_id: int) -> None:
    """Email every qualifying match in one message, and record what this run did.

    The run's count is written on the row `run_id` names, in every case. A
    selection with nothing in it writes `0` -- the run emailed nothing -- and does
    nothing else: no `send`, no marker, no other row touched, which is why the
    ordinary "nothing new" run stays a success and why its row says so. A send
    that returned writes the number of matches in the one message that went out,
    with the markers, so the row always describes its own run and never a stale
    count from an earlier one. A `send` that raises writes nothing at all -- the
    count stays NULL, which means "unknown" -- and the exception escapes, so the
    same matches go out with the next run.
    """
    matches = _selected()
    if matches:
        mailer.send(subject=DIGEST_SUBJECT, body=_message_body(matches))
    _record(run_id, matches)


def _selected() -> list[DigestMatch]:
    """Every un-notified score at or above the cutoff, newest first, as plain values.

    One statement, one session, over the three tables the message text is built
    from: the match, the posting it scores and the company that posting belongs
    to. `match.created_at` descending is the age of the match the email lists,
    and `match.id` descending breaks a tie between two rows written in the same
    instant.

    The row qualifies by its own `outcome`: `'scored'` is the one value that means
    the model answered, and the negation of it -- `outcome != 'scored'` -- is the
    rule the two reason prefixes used to stand for. The `NOT NULL` check makes
    `'scored'` the whole of it: a row is rejected, not scored, or scored.
    """
    cutoff = min_score()
    statement = (
        select(Match, Posting, Company)
        .join(Posting, Match.posting_id == Posting.id)
        .join(Company, Posting.company_id == Company.id)
        .where(Match.notified_at.is_(None))
        .where(Match.score >= cutoff)
        .where(Match.outcome == "scored")
        .order_by(Match.created_at.desc(), Match.id.desc())
    )
    with db.session_scope() as session:
        return [
            DigestMatch(
                id=match.id,
                company=company.name,
                title=posting.title,
                url=posting.url,
                score=match.score,
                reason=match.reason,
                gaps=_gaps(match.gaps),
            )
            for match, posting, company in session.execute(statement)
        ]


def _gaps(stored: str | None) -> list[str]:
    """The gap strings #8 wrote as JSON text, or none on a row that carries no gaps."""
    return json.loads(stored) if stored else []


def _message_body(matches: list[DigestMatch]) -> str:
    """The whole message: one block per match, in the order the matches arrived."""
    return "\n\n".join(_match_block(match) for match in matches)


def _match_block(match: DigestMatch) -> str:
    """One match as the lines a reader scans: who, what, where, how well, why, what is missing."""
    lines = [
        f"{match.company}: {match.title}",
        f"{_URL_LABEL}{match.url}",
        f"{_SCORE_LABEL}{match.score}",
        f"{_REASON_LABEL}{match.reason}",
    ]
    lines.extend(f"{_GAP_LABEL}{gap}" for gap in match.gaps)
    return "\n".join(lines)


def _record(run_id: int, matches: list[DigestMatch]) -> None:
    """Write this run's own count, and the marker on the matches that went out: one unit of work.

    The count is the list's length -- `0` when nothing qualified and nothing was
    sent, which is a real answer rather than a missing one -- and it is written
    before anything the send could have changed, because this is only reached once
    `send` returned. Only the matches that went out are marked, and only their
    `notified_at` is written; the instant is read here, after the send, so a
    failure above leaves the rows as they were and re-emails them.

    A run row that is no longer there -- deleted while the run was in flight -- is
    nothing to write and not an error, the way `internal._end_run` treats it.
    """
    with db.session_scope() as session:
        run = session.get(Run, run_id)
        if run is not None:
            run.matches_emailed = len(matches)
        if matches:
            session.execute(
                update(Match)
                .where(Match.id.in_([match.id for match in matches]))
                .values(notified_at=datetime.now(UTC))
            )
