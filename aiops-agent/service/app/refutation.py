"""Checking an answer against what a person already ruled out.

Day39 measured the obvious design and found it backwards. Injecting
"already ruled out: a code regression in order-service itself" into the prompt
produced, in three seeds out of three, the answer "Code regression in
order-service v1.8.2 causing auth failures" — the refuted hypothesis, restated,
at *lower* confidence than the arm that never saw it. Naming a branch appears to
make it salient; the negation does not survive the trip.

So a refuted hypothesis is used here instead of there. Dead ends that a machine
can enforce (a query that returns nothing, an action a person declined) stay in
front of the model, because those are enforced elsewhere and nothing is left to
the model's cooperation. A refuted *hypothesis* cannot be enforced — it is not a
tool call — and it is now checked against the answer after the fact, where a hit
sends the answer back instead of hoping the model avoided it.

The matching is deliberately dumb and explainable. Both sides are free text
written by people and models, so there is no key to join on; what there is, is
the distinctive words. "Because it repeats most of what the person crossed out"
is a sentence you can say to whoever is on call. A similarity score is not.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("aiops_agent.refutation")

# Words that carry no discriminating power in this domain. Kept short on
# purpose: the risk of a long list is that it eats the words that made two
# hypotheses different in the first place.
_STOPWORDS = frozenset(
    """
    a an and are as at be been being by caused causes causing code did do does due
    for from had has have in into is it its of on or that the their there this to
    was were will with within issue issues problem problems root cause error errors
    """.split()
)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*")

# How much of the refuted hypothesis has to reappear before we call it a repeat.
# 0.6 leaves room for an answer that reaches the same area from a different
# direction while catching one that restates it.
MATCH_THRESHOLD = 0.6


def significant_words(text: str) -> set[str]:
    """Lowercased words with the connective tissue removed."""
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS}


def overlap(subject: str, answer: str) -> float:
    """How much of `subject` reappears in `answer`, 0.0-1.0.

    Asymmetric on purpose: a long answer that happens to contain the whole
    refuted hypothesis is a repeat, and dividing by the answer's length would
    hide that behind everything else the answer said.
    """
    want = significant_words(subject)
    if not want:
        return 0.0
    return len(want & significant_words(answer)) / len(want)


def find_repeat(answer: str, refuted: list[dict]) -> dict | None:
    """The refuted hypothesis this answer repeats, if any. Best match wins.

    `refuted` is rows from `store.case_ruled_out_for`; only `kind='hypothesis'`
    rows are considered, because a ruled-out *query* being mentioned in an
    answer is not a wrong answer.
    """
    best, best_score = None, 0.0
    for row in refuted:
        if row.get("kind") != "hypothesis":
            continue
        score = overlap(row.get("subject", ""), answer)
        if score >= MATCH_THRESHOLD and score > best_score:
            best, best_score = row, score
    if best is not None:
        logger.info(
            "answer repeats a refuted hypothesis (%.2f): %s", best_score, best.get("subject")
        )
    return best


def retry_prompt(row: dict) -> str:
    """What to send back. Says what was crossed out and by whom, asks for a
    different line of reasoning, and does not say what the answer is — nobody
    here knows that, and pretending otherwise is how a correction turns into a
    leak."""
    who = "a person" if row.get("disproved_by") == "human" else row.get("disproved_by", "someone")
    evidence = (row.get("evidence") or "").strip()
    because = f" ({evidence})" if evidence else ""
    return (
        f"Your conclusion restates a hypothesis that {who} already investigated and "
        f"ruled out on this incident: {row.get('subject')!r}{because}. That does not make "
        "your evidence wrong, but it does mean this answer cannot be the conclusion. "
        "Re-examine what you found and look for a cause this does not cover — check the "
        "services this one depends on if you have not. If after that you still believe "
        "the ruled-out explanation, say so explicitly and state what new evidence "
        "contradicts the earlier finding."
    )
