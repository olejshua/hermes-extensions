"""Deterministic Deep-phase scoring and explainable promotion gates.

The signal philosophy follows OpenClaw: relevance and evidence quality,
reinforcement frequency, independent-source diversity, recency, multi-day
consolidation, and durability. The important local difference is that gates do
not short-circuit. Every gate is evaluated and recorded, so ``dream-status``
can answer "why did this not promote?" with the full picture rather than only
the first failure.

Scoring runs before the model sees anything. The model's structured review can
only confirm or downgrade a deterministic classification, never upgrade it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .config import Thresholds
from .store import CandidateRecord, EvidenceRecord

CANONICAL_ROLE = "canonical"
USER_ROLE = "user"

_SUPPORT_STOPWORDS = frozenset({
    "about", "after", "again", "also", "and", "assistant", "because", "been", "being",
    "dass", "der", "die", "eine", "einer", "eines", "for", "from", "haben", "hermes",
    "ihm", "ihn", "ist", "mit", "nicht", "oder", "seine", "seiner", "sich",
    "that", "the", "their", "this", "user", "von", "want", "wants", "was", "were", "will",
    "with", "would", "und", "zur", "zum",
})

# Classifications, ordered weakest to strongest. The model may move an item
# down this ladder but never up.
CLASSIFICATION_ORDER = ("reject", "defer", "hypothesis", "context_inbox", "openviking_dream")


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    actual: Any
    required: Any

    def as_dict(self) -> dict[str, Any]:
        return {"gate": self.name, "passed": self.passed, "actual": self.actual, "required": self.required}


@dataclass
class Signals:
    relevance: float = 0.0
    evidence_quality: float = 0.0
    frequency: float = 0.0
    diversity: float = 0.0
    recency: float = 0.0
    consolidation: float = 0.0
    durability: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "relevance": round(self.relevance, 4),
            "evidence_quality": round(self.evidence_quality, 4),
            "frequency": round(self.frequency, 4),
            "diversity": round(self.diversity, 4),
            "recency": round(self.recency, 4),
            "consolidation": round(self.consolidation, 4),
            "durability": round(self.durability, 4),
        }


@dataclass
class ScoredCandidate:
    candidate: CandidateRecord
    evidence: tuple[EvidenceRecord, ...]
    signals: Signals
    score: float
    classification: str
    gates: tuple[Gate, ...]
    facts: dict[str, Any] = field(default_factory=dict)

    def explain(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "classification": self.classification,
            "signals": self.signals.as_dict(),
            "gates": [gate.as_dict() for gate in self.gates],
            "facts": self.facts,
        }

    @property
    def failed_gates(self) -> list[str]:
        return [gate.name for gate in self.gates if not gate.passed]


def score_candidate(
    candidate: CandidateRecord,
    evidence: Sequence[EvidenceRecord],
    thresholds: Thresholds,
    *,
    now: float,
    corroborating_refs: frozenset[str] = frozenset(),
) -> ScoredCandidate:
    """Score one candidate and classify it against every gate."""

    unique_refs = {item.ref for item in evidence}
    unique_sessions = {(item.profile, item.session_id) for item in evidence}
    unique_profiles = {item.profile for item in evidence}
    unique_days = {item.day for item in evidence if item.day and item.day != "unknown"}
    roles = {item.role for item in evidence}
    claim_terms, overlap_terms, support_coverage = lexical_support(candidate.claim, evidence)
    required_overlap = 1 if len(claim_terms) <= 4 else 2
    lexical_supported = len(overlap_terms) >= required_overlap and support_coverage >= 0.2

    has_user_evidence = USER_ROLE in roles
    # ``corroborating_refs`` are exact, role-checked canonical REM citations
    # deterministically linked to this candidate. They are intentionally not
    # copied into the candidate's own session evidence rows.
    has_canonical = CANONICAL_ROLE in roles or bool(corroborating_refs)
    corroborated = has_user_evidence or has_canonical

    age_days = max(0.0, (now - candidate.last_seen) / 86_400.0)
    span_days = max(0.0, (candidate.last_seen - candidate.first_seen) / 86_400.0)

    signals = Signals(
        relevance=_clamp(candidate.confidence),
        evidence_quality=_evidence_quality(evidence, has_user_evidence, has_canonical),
        frequency=_clamp(math.log1p(candidate.reinforcement) / math.log1p(6)),
        diversity=_diversity(len(unique_sessions), len(unique_profiles), len(unique_refs)),
        recency=_recency(age_days, thresholds.recency_half_life_days),
        consolidation=_consolidation(len(unique_days), span_days),
        durability=_durability(candidate.durability, candidate.actionability, candidate.kind),
    )

    weights = thresholds.normalized_weights()
    score = _clamp(
        signals.relevance * weights["relevance"]
        + signals.evidence_quality * weights["evidence_quality"]
        + signals.frequency * weights["frequency"]
        + signals.diversity * weights["diversity"]
        + signals.recency * weights["recency"]
        + signals.consolidation * weights["consolidation"]
        + signals.durability * weights["durability"]
    )

    gates = (
        Gate("score", score >= thresholds.promote_min_score, round(score, 4), thresholds.promote_min_score),
        Gate("evidence_count", len(unique_refs) >= thresholds.promote_min_evidence, len(unique_refs), thresholds.promote_min_evidence),
        Gate(
            "unique_sessions",
            len(unique_sessions) >= thresholds.promote_min_unique_sessions,
            len(unique_sessions),
            thresholds.promote_min_unique_sessions,
        ),
        Gate("unique_days", len(unique_days) >= thresholds.promote_min_unique_days, len(unique_days), thresholds.promote_min_unique_days),
        Gate(
            "corroboration",
            corroborated or not thresholds.promote_require_user_or_canonical,
            "user" if has_user_evidence else ("canonical" if has_canonical else "assistant_only"),
            "user_or_canonical" if thresholds.promote_require_user_or_canonical else "not_required",
        ),
        Gate(
            "lexical_support",
            lexical_supported,
            {"overlap": sorted(overlap_terms), "coverage": round(support_coverage, 4)},
            {"min_terms": required_overlap, "min_coverage": 0.2},
        ),
    )

    classification = _classify(score, gates, thresholds, len(unique_refs), candidate)

    facts = {
        "candidate_id": candidate.candidate_id,
        "kind": candidate.kind,
        "reinforcement": candidate.reinforcement,
        "unique_refs": len(unique_refs),
        "unique_sessions": len(unique_sessions),
        "unique_profiles": sorted(unique_profiles),
        "unique_days": sorted(unique_days),
        "roles": sorted(roles),
        "assistant_only": not corroborated,
        "age_days": round(age_days, 3),
        "span_days": round(span_days, 3),
        "claim_support_terms": sorted(claim_terms),
        "evidence_overlap_terms": sorted(overlap_terms),
    }

    return ScoredCandidate(
        candidate=candidate,
        evidence=tuple(evidence),
        signals=signals,
        score=score,
        classification=classification,
        gates=gates,
        facts=facts,
    )


def _classify(
    score: float,
    gates: Sequence[Gate],
    thresholds: Thresholds,
    evidence_count: int,
    candidate: CandidateRecord,
) -> str:
    if all(gate.passed for gate in gates):
        return "openviking_dream"

    corroboration_ok = next(gate.passed for gate in gates if gate.name == "corroboration")

    # Actionable, corroborated, but thin on independent evidence: surface it
    # where the user will actually see it instead of burying it.
    if (
        score >= thresholds.inbox_min_score
        and evidence_count >= thresholds.inbox_min_evidence
        and corroboration_ok
        and candidate.actionability in ("act", "watch")
    ):
        return "context_inbox"

    if score >= thresholds.hypothesis_min_score:
        return "hypothesis"

    if score <= thresholds.reject_max_score:
        return "reject"

    return "defer"


def apply_model_review(scored: ScoredCandidate, verdict: str | None, rationale: str) -> tuple[str, str]:
    """Combine deterministic classification with the model's review.

    The model can only move a candidate *down* the ladder. An eloquent argument
    for promotion does not override the evidence gates.
    """

    deterministic = scored.classification
    if not verdict or verdict not in CLASSIFICATION_ORDER:
        return deterministic, rationale
    if CLASSIFICATION_ORDER.index(verdict) < CLASSIFICATION_ORDER.index(deterministic):
        return verdict, rationale or "downgraded by model review"
    return deterministic, rationale


def _evidence_quality(evidence: Sequence[EvidenceRecord], has_user: bool, has_canonical: bool) -> float:
    if not evidence:
        return 0.0
    # Substance: very short snippets rarely support a durable claim.
    lengths = [len(item.snippet.strip()) for item in evidence]
    substance = _clamp(sum(min(1.0, length / 160.0) for length in lengths) / len(lengths))
    provenance = 0.0
    if has_user:
        provenance += 0.6
    if has_canonical:
        provenance += 0.4
    return _clamp(0.5 * substance + 0.5 * _clamp(provenance))


def lexical_support(
    claim: str, evidence: Sequence[EvidenceRecord],
) -> tuple[set[str], set[str], float]:
    """Conservative deterministic guard against unrelated real citations."""

    def terms(text: str) -> set[str]:
        return {
            token for token in re.findall(r"\w+", text.casefold(), flags=re.UNICODE)
            if len(token) >= 3 and token not in _SUPPORT_STOPWORDS and not token.isdigit()
        }

    claim_terms = terms(claim)
    evidence_terms: set[str] = set()
    for item in evidence:
        evidence_terms.update(terms(item.snippet))
    overlap = claim_terms & evidence_terms
    coverage = len(overlap) / max(1, min(len(claim_terms), 8))
    return claim_terms, overlap, coverage


def _diversity(unique_sessions: int, unique_profiles: int, unique_refs: int) -> float:
    session_component = _clamp(unique_sessions / 4.0)
    profile_component = _clamp((unique_profiles - 1) / 2.0)
    ref_component = _clamp(unique_refs / 6.0)
    return _clamp(0.55 * session_component + 0.25 * profile_component + 0.20 * ref_component)


def _recency(age_days: float, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 1.0
    return _clamp(math.exp(-math.log(2) / half_life_days * age_days))


def _consolidation(unique_days: int, span_days: float) -> float:
    if unique_days <= 0:
        return 0.0
    if unique_days == 1:
        return 0.2
    day_component = _clamp(math.log1p(unique_days - 1) / math.log1p(4))
    span_component = _clamp(span_days / 7.0)
    return _clamp(0.55 * day_component + 0.45 * span_component)


def _durability(durability: str, actionability: str, kind: str) -> float:
    base = {"durable": 1.0, "seasonal": 0.55, "ephemeral": 0.15}.get(durability, 0.15)
    if kind in ("decision", "preference", "commitment", "constraint"):
        base = min(1.0, base + 0.15)
    if actionability == "act":
        base = min(1.0, base + 0.1)
    return _clamp(base)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))
