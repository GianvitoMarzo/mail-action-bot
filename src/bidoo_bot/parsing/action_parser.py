"""Scoring links to find the "redeem your free bid" action.

Scoring model
-------------
Every configured rule that matches contributes its weight. Positive weights are
combined with a *noisy-OR*::

    confidence = 1 - product(1 - w_i)

so several weak hints reinforce each other while a single one is never enough,
and the result stays in ``[0, 1]``. Negative rules then scale the result down::

    confidence *= product(1 - w_j)

The output is deliberately explainable: every candidate carries the list of
rules that fired, which is what ``bidoo-bot analyze-email`` prints.

Refusing is always an option. ``LOW_CONFIDENCE`` and ``AMBIGUOUS`` are normal
outcomes, not errors: not redeeming a bid is much cheaper than following the
wrong link.
"""

from __future__ import annotations

from math import prod

from bidoo_bot.config import ParserSettings
from bidoo_bot.models.candidate import ActionCandidate, ParseResult, ParseStatus
from bidoo_bot.parsing.html import LinkFeatures, canonical_url, extract_links, linkify_plaintext

#: How many ranked candidates are reported back (for debugging output).
MAX_REPORTED_CANDIDATES = 10


class ActionParser:
    """Finds the most plausible action link inside an email body."""

    def __init__(self, settings: ParserSettings) -> None:
        self._settings = settings

    @property
    def settings(self) -> ParserSettings:
        return self._settings

    # -- public API ---------------------------------------------------------

    def parse(self, html: str, *, text_body: str = "") -> ParseResult:
        """Analyse ``html`` (or a plain-text fallback) and rank the links."""
        body = html
        if not body.strip():
            body = linkify_plaintext(text_body)
        if not body.strip():
            return ParseResult(status=ParseStatus.NO_BODY, detail="message has no usable body")

        links = extract_links(
            body,
            max_links=self._settings.max_links,
            context_chars=self._settings.context_chars,
        )
        if not links:
            return ParseResult(
                status=ParseStatus.NO_LINKS, detail="no absolute http(s) link found in the body"
            )

        ranked = self._rank(links)
        reported = tuple(ranked[:MAX_REPORTED_CANDIDATES])
        best = ranked[0]

        if best.confidence < self._settings.min_confidence:
            return ParseResult(
                status=ParseStatus.LOW_CONFIDENCE,
                detail=(
                    f"best candidate scored {best.confidence:.2f}, "
                    f"below min_confidence {self._settings.min_confidence:.2f}"
                ),
                best=best,
                candidates=reported,
            )

        rival = self._closest_rival(ranked)
        if rival is not None:
            return ParseResult(
                status=ParseStatus.AMBIGUOUS,
                detail=(
                    f"two distinct links score within {self._settings.ambiguity_margin:.2f} "
                    f"({best.confidence:.2f} vs {rival.confidence:.2f}); refusing to guess"
                ),
                best=best,
                candidates=reported,
            )

        return ParseResult(
            status=ParseStatus.OK,
            detail=f"single best candidate at {best.confidence:.2f}",
            best=best,
            candidates=reported,
        )

    def score_link(self, link: LinkFeatures) -> ActionCandidate:
        """Score one extracted link. Exposed for tests and tooling."""
        haystacks = {
            "text": link.text,
            "url": link.url,
            "attrs": link.attrs,
            "context": link.context,
            # "any" means "anything about this link itself" and deliberately
            # excludes the context: in a short email every link shares the same
            # surroundings, so an "unsubscribe" in the footer would otherwise
            # penalise the real button too.
            "any": " ".join(x for x in (link.text, link.url, link.attrs) if x),
        }

        positive: list[tuple[str, float]] = []
        negative: list[tuple[str, float]] = []
        for rule in self._settings.signals:
            haystack = haystacks[rule.field]
            if not haystack or not rule.pattern.search(haystack):
                continue
            (negative if rule.negative else positive).append((rule.name, rule.weight))

        base = 1.0 - prod(1.0 - weight for _, weight in positive) if positive else 0.0
        penalty = prod(1.0 - weight for _, weight in negative) if negative else 1.0
        confidence = round(max(0.0, min(1.0, base * penalty)), 4)

        return ActionCandidate(
            text=link.text,
            url=link.url,
            confidence=confidence,
            reason=_format_reason(positive, negative, base, penalty),
            signals=tuple(name for name, _ in positive) + tuple(f"-{name}" for name, _ in negative),
            position=link.index,
        )

    # -- internals ----------------------------------------------------------

    def _rank(self, links: list[LinkFeatures]) -> list[ActionCandidate]:
        """Score, de-duplicate by target URL, and sort by confidence."""
        best_by_url: dict[str, ActionCandidate] = {}
        for link in links:
            candidate = self.score_link(link)
            key = canonical_url(candidate.url)
            current = best_by_url.get(key)
            if current is None or (candidate.confidence, -candidate.position) > (
                current.confidence,
                -current.position,
            ):
                best_by_url[key] = candidate
        return sorted(best_by_url.values(), key=lambda c: (-c.confidence, c.position))

    def _closest_rival(self, ranked: list[ActionCandidate]) -> ActionCandidate | None:
        """Return the runner-up when it is too close to the winner to choose."""
        if len(ranked) < 2:
            return None
        best, second = ranked[0], ranked[1]
        if canonical_url(second.url) == canonical_url(best.url):  # pragma: no cover - deduped
            return None
        if best.confidence - second.confidence < self._settings.ambiguity_margin:
            return second
        return None


def _format_reason(
    positive: list[tuple[str, float]],
    negative: list[tuple[str, float]],
    base: float,
    penalty: float,
) -> str:
    if not positive and not negative:
        return "no signal matched"
    parts: list[str] = []
    if positive:
        matched = ", ".join(f"{name}(+{weight:.2f})" for name, weight in positive)
        parts.append(f"matched {matched} -> {base:.2f}")
    else:
        parts.append("no positive signal matched")
    if negative:
        penalised = ", ".join(f"{name}(-{weight:.2f})" for name, weight in negative)
        parts.append(f"penalised by {penalised} (x{penalty:.2f})")
    return "; ".join(parts)
