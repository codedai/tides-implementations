"""Experimental score contract: registry, transforms, and decisions.

The numerical parameters here are NOT calibrated and NOT approved
(status = experimental on every feature). What this module fixes is the
*mechanics*: registered-features-only, integer fixed-point scores,
explicit missingness, regime switching without renormalization, and
threshold+margin acceptance with abstention.

Firewall: passenger quantities and legacy identity fields can never
enter a score. See ``contracts.EXCLUDED_IDENTITY_FEATURES``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import yaml

from .contracts import (
    EXCLUDED_IDENTITY_FEATURES,
    Decision,
    EvidenceRegime,
    HardValidity,
    Reason,
    ScoredCandidate,
)
from .fixedpoint import (
    SCORE_SCALE,
    interpolate_piecewise,
    quantize,
    weighted_mean_q,
)

VALID_MISSING_MODES = {"neutral", "regime_switch", "hard_invalid"}
VALID_DECISIONS = {"segment_selection", "apc_attachment", "stage1_support"}


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    family: str
    unit: str
    direction: str  # lower_better | higher_better
    quantum: Fraction
    transform_points: tuple[tuple[int, int], ...]
    hard_bound_min: int | None
    hard_bound_max: int | None
    hard_bound_reason: str | None
    missing_mode: str
    missing_neutral_q: int | None
    decisions: frozenset[str]
    regimes: frozenset[str]
    aggregation_grain: str
    aggregation_order: str
    weight: int
    reason_code: str
    status: str
    note: str


@dataclass(frozen=True)
class Thresholds:
    absolute_q: int
    margin_q: int


class ScoreContract:
    """One loaded, validated variant of the experimental score contract."""

    def __init__(self, raw: dict, variant: str):
        self.variant = variant
        self.raw = raw
        self.contract_name = raw["contract_name"]
        self.schema_version = raw["schema_version"]
        self.status = raw["status"]
        if self.status != "experimental":
            raise ValueError(
                "the spike refuses any score contract not marked experimental"
            )
        if raw.get("score_scale") != SCORE_SCALE:
            raise ValueError("score_scale must equal fixedpoint.SCORE_SCALE")
        variants = raw["variants"]
        if variant not in variants:
            raise KeyError(f"unknown score-contract variant: {variant}")
        vraw = variants[variant]
        self.thresholds: dict[str, Thresholds] = {
            d: Thresholds(int(t["absolute_q"]), int(t["margin_q"]))
            for d, t in vraw["thresholds"].items()
        }
        self.null_scores: dict[str, dict[str, int]] = {
            d: {k: int(v) for k, v in m.items()} for d, m in vraw["null_scores"].items()
        }
        # family_weights[decision][regime][family] -> integer weight
        self.family_weights: dict[str, dict[str, dict[str, int]]] = {
            decision: {
                regime: {f: int(w) for f, w in fams.items()}
                for regime, fams in regimes.items()
            }
            for decision, regimes in vraw["family_weights"].items()
        }
        # null_family_q[decision][family] -> declared prior when a family
        # is absent by construction (e.g. schedule family on a
        # NO_SCHEDULED_TRIP tuple, spatial family without geometry).
        self.null_family_q: dict[str, dict[str, int]] = {
            decision: {k: int(v) for k, v in fams.items()}
            for decision, fams in vraw.get("null_family_q", {}).items()
        }
        overrides = vraw.get("feature_overrides", {})
        self.features: dict[str, FeatureSpec] = {}
        for name, spec in raw["features"].items():
            merged = dict(spec)
            merged.update(overrides.get(name, {}))
            self.features[name] = self._parse_feature(name, merged)
        self._validate_firewall()

    @staticmethod
    def _parse_feature(name: str, spec: dict) -> FeatureSpec:
        decisions = frozenset(spec["decisions"])
        if not decisions <= VALID_DECISIONS:
            raise ValueError(f"{name}: unknown decision in {sorted(decisions)}")
        if spec["missing"]["mode"] not in VALID_MISSING_MODES:
            raise ValueError(f"{name}: unknown missing mode")
        if spec["missing"]["mode"] == "neutral" and (
            spec["missing"].get("neutral_q") is None
        ):
            raise ValueError(f"{name}: neutral missing mode requires neutral_q")
        points = tuple((int(x), int(y)) for x, y in spec["transform"]["points"])
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        if xs != sorted(set(xs)):
            raise ValueError(f"{name}: transform raw units not increasing")
        if any(not 0 <= y <= SCORE_SCALE for y in ys):
            raise ValueError(f"{name}: transform qscore outside [0, scale]")
        direction = spec["direction"]
        if direction == "lower_better" and ys != sorted(ys, reverse=True):
            raise ValueError(f"{name}: lower_better transform not monotone")
        if direction == "higher_better" and ys != sorted(ys):
            raise ValueError(f"{name}: higher_better transform not monotone")
        hb = spec.get("hard_bound") or {}
        weight = int(spec["weight"])
        if weight < 0:
            raise ValueError(f"{name}: negative weight")
        return FeatureSpec(
            name=name,
            family=spec["family"],
            unit=spec["unit"],
            direction=direction,
            quantum=Fraction(str(spec["quantum"])),
            transform_points=points,
            hard_bound_min=hb.get("min"),
            hard_bound_max=hb.get("max"),
            hard_bound_reason=hb.get("reason_code"),
            missing_mode=spec["missing"]["mode"],
            missing_neutral_q=spec["missing"].get("neutral_q"),
            decisions=decisions,
            regimes=frozenset(spec["regimes"]),
            aggregation_grain=spec["aggregation"]["grain"],
            aggregation_order=spec["aggregation"]["order"],
            weight=weight,
            reason_code=spec["reason_code"],
            status=spec["status"],
            note=spec.get("note", ""),
        )

    def _validate_firewall(self) -> None:
        for name in self.features:
            if name.lower() in EXCLUDED_IDENTITY_FEATURES:
                raise ValueError(
                    f"firewalled feature {name!r} may not be registered in "
                    "an identity/attachment score contract"
                )

    # --- scoring ---

    def feature_qscore(
        self, spec: FeatureSpec, value: float | int | None
    ) -> tuple[int | None, str | None, bool]:
        """Return (qscore, reason, hard_invalid) for one feature value.

        qscore None means the feature is absent for this candidate and
        the caller must apply the declared missing mode (regime switch
        happens at candidate level, never by weight renormalization).
        """
        if value is None:
            if spec.missing_mode == "neutral":
                return spec.missing_neutral_q, None, False
            if spec.missing_mode == "hard_invalid":
                return None, spec.hard_bound_reason or spec.reason_code, True
            return None, None, False  # regime_switch: handled by caller
        raw_q = quantize(value, spec.quantum)
        if spec.hard_bound_min is not None and raw_q < spec.hard_bound_min:
            return None, spec.hard_bound_reason, True
        if spec.hard_bound_max is not None and raw_q > spec.hard_bound_max:
            return None, spec.hard_bound_reason, True
        return interpolate_piecewise(list(spec.transform_points), raw_q), None, False

    def score_candidate(
        self,
        decision: str,
        feature_vector: dict[str, float | int | None],
        regime: EvidenceRegime,
        *,
        extra_family_q: dict[str, int] | None = None,
    ) -> tuple[int, dict[str, int], list[str], bool]:
        """Score one candidate's feature vector under one regime.

        Returns (final_q, family_qscores, reasons, hard_invalid).

        Raises on firewalled or unregistered feature names: unregistered
        features must never leak into a score.
        """
        for key in feature_vector:
            if key.lower() in EXCLUDED_IDENTITY_FEATURES:
                raise ValueError(f"firewalled feature {key!r} in scoring input")
            if key not in self.features:
                raise ValueError(f"unregistered feature {key!r} in scoring input")
        families: dict[str, list[tuple[int, int]]] = {}
        reasons: list[str] = []
        hard_invalid = False
        for name in sorted(feature_vector):  # canonical order
            spec = self.features[name]
            if decision not in spec.decisions:
                continue
            if regime.value not in spec.regimes:
                continue
            q, reason, invalid = self.feature_qscore(spec, feature_vector[name])
            if invalid:
                hard_invalid = True
                if reason:
                    reasons.append(reason)
                continue
            if q is None:
                # regime_switch missing: the caller chose the regime; a
                # required-for-this-regime feature that is still missing
                # means the candidate cannot be scored in this regime.
                raise ValueError(
                    f"feature {name!r} missing under regime {regime}: "
                    "caller must select the other regime, not renormalize"
                )
            families.setdefault(spec.family, []).append((q, spec.weight))
        family_q: dict[str, int] = {
            fam: weighted_mean_q(pairs) for fam, pairs in sorted(families.items())
        }
        if extra_family_q:
            family_q.update(extra_family_q)
        fam_weights = self.family_weights[decision][regime.value]
        null_family_q = self.null_family_q.get(decision, {})
        pairs = []
        for fam, w in sorted(fam_weights.items()):
            if fam in family_q:
                pairs.append((family_q[fam], w))
            elif fam in null_family_q:
                # A family absent by construction (e.g. schedule family on
                # a NO_SCHEDULED_TRIP tuple) takes its declared prior —
                # explicit, not renormalized away.
                pairs.append((null_family_q[fam], w))
                family_q[fam] = null_family_q[fam]
            else:
                raise ValueError(
                    f"family {fam!r} missing and no null_family_q declared"
                )
        final_q = weighted_mean_q(pairs)
        return final_q, family_q, reasons, hard_invalid


def decide(
    subject_id: str,
    candidates: list[ScoredCandidate],
    thresholds: Thresholds,
    *,
    status_map: dict[str, str],
) -> Decision:
    """Threshold-and-margin decision over a complete candidate set.

    ``status_map`` maps abstract outcomes (accepted, null_winner,
    low_score, low_margin, evidence_tie, no_candidate, hard_invalid_*)
    to the caller's status vocabulary.

    Candidate identity (keys) affects serialization order only; two
    evidence-identical candidates always produce margin_q = 0 and an
    abstention, never a winner chosen by ID.
    """
    non_null = [c for c in candidates if not c.is_null]
    nulls = [c for c in candidates if c.is_null]
    null_q = max((c.qscore for c in nulls), default=None)
    valid = [c for c in non_null if c.hard_validity == HardValidity.VALID]
    all_reasons: list[str] = sorted({r for c in candidates for r in c.reason_codes})

    if not non_null:
        return Decision(
            subject_id=subject_id,
            status=status_map["no_candidate"],
            selected_key=None,
            best_q=None,
            runner_up_q=None,
            null_q=null_q,
            margin_q=None,
            n_candidates=len(candidates),
            n_hard_valid=0,
            reason_codes=tuple(all_reasons),
        )

    if not valid:
        # Every real candidate is hard-invalid: report the dominant hard
        # reason deterministically (sorted first among most frequent).
        # Only reasons the caller mapped as hard_invalid:* statuses may
        # become dominant — incidental annotations (e.g. a stop-mapping
        # ambiguity note) must not outrank the true blocking reason.
        reason_counts: dict[str, int] = {}
        for c in non_null:
            for r in c.reason_codes:
                if f"hard_invalid:{r}" in status_map:
                    reason_counts[r] = reason_counts.get(r, 0) + 1
        dominant = (
            min(reason_counts, key=lambda r: (-reason_counts[r], r))
            if reason_counts
            else Reason.NO_SEGMENT_CANDIDATE
        )
        return Decision(
            subject_id=subject_id,
            status=status_map.get(
                f"hard_invalid:{dominant}", status_map["all_hard_invalid"]
            ),
            selected_key=None,
            best_q=None,
            runner_up_q=None,
            null_q=null_q,
            margin_q=None,
            n_candidates=len(candidates),
            n_hard_valid=0,
            reason_codes=tuple(all_reasons),
        )

    best_q = max(c.qscore for c in valid)
    top = [c for c in valid if c.qscore == best_q]
    others = [c for c in valid if c.qscore < best_q]
    second_q = max((c.qscore for c in others), default=None)

    # Runner-up for the margin: the better of the second non-null and the
    # null alternative. A sole non-null candidate competes with the null.
    contenders = [q for q in (second_q, null_q) if q is not None]
    runner_up = max(contenders) if contenders else None

    if null_q is not None and null_q >= best_q:
        return Decision(
            subject_id=subject_id,
            status=status_map["null_winner"],
            selected_key=None,
            best_q=best_q,
            runner_up_q=runner_up,
            null_q=null_q,
            margin_q=best_q - null_q,
            n_candidates=len(candidates),
            n_hard_valid=len(valid),
            reason_codes=tuple(sorted({*all_reasons, Reason.NULL_WINNER})),
        )

    if len(top) > 1:
        return Decision(
            subject_id=subject_id,
            status=status_map["evidence_tie"],
            selected_key=None,
            best_q=best_q,
            runner_up_q=best_q,
            null_q=null_q,
            margin_q=0,
            n_candidates=len(candidates),
            n_hard_valid=len(valid),
            reason_codes=tuple(sorted({*all_reasons, Reason.SCORE_EVIDENCE_TIE})),
        )

    margin_q = best_q - runner_up if runner_up is not None else best_q
    winner = top[0]

    if best_q < thresholds.absolute_q:
        return Decision(
            subject_id=subject_id,
            status=status_map["low_score"],
            selected_key=None,
            best_q=best_q,
            runner_up_q=runner_up,
            null_q=null_q,
            margin_q=margin_q,
            n_candidates=len(candidates),
            n_hard_valid=len(valid),
            reason_codes=tuple(sorted({*all_reasons, Reason.SCORE_LOW})),
        )
    if margin_q < thresholds.margin_q:
        return Decision(
            subject_id=subject_id,
            status=status_map["low_margin"],
            selected_key=None,
            best_q=best_q,
            runner_up_q=runner_up,
            null_q=null_q,
            margin_q=margin_q,
            n_candidates=len(candidates),
            n_hard_valid=len(valid),
            reason_codes=tuple(sorted({*all_reasons, Reason.SCORE_LOW_MARGIN})),
        )
    return Decision(
        subject_id=subject_id,
        status=status_map["accepted"],
        selected_key=winner.key,
        best_q=best_q,
        runner_up_q=runner_up,
        null_q=null_q,
        margin_q=margin_q,
        n_candidates=len(candidates),
        n_hard_valid=len(valid),
        reason_codes=tuple(all_reasons),
        regime=winner.regime,
    )


def load_contract(path: Path, variant: str) -> ScoreContract:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ScoreContract(raw, variant)


def contract_hash(path: Path) -> str:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
