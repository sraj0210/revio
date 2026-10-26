"""Deterministic finding<->label matching.

A finding matches a labeled defect when it is on the same file and its line falls within a window of
the label's line (optionally also requiring category agreement, compared after the same
normalization the service applies to finding categories). Matching is one-to-one and *maximum*:
each finding and each label is used at most once, and the pairing credits as many labels as any
pairing could, independent of the order labels or findings are listed in. Findings without a line
("unanchored" summary findings) cannot match a line label; they are reported separately and are
neither credited nor counted as false positives, so general summary prose does not distort
precision.
"""

from dataclasses import dataclass, field

from evals.harness.cases import FindingLite, Label
from venaroq.domain.finding_identity import normalize_category


@dataclass(frozen=True)
class MatchSettings:
    line_window: int = 3
    require_category: bool = False

    def __post_init__(self) -> None:
        if self.line_window < 0:
            raise ValueError("line_window must be >= 0")


def _labels() -> list[Label]:
    return []


def _findings() -> list[FindingLite]:
    return []


@dataclass
class CaseMatch:
    matched_labels: list[Label] = field(default_factory=_labels)
    missed_labels: list[Label] = field(default_factory=_labels)
    matched_findings: list[FindingLite] = field(default_factory=_findings)
    false_positive_findings: list[FindingLite] = field(default_factory=_findings)
    unanchored_findings: list[FindingLite] = field(default_factory=_findings)


def _category(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return normalize_category(value)
    except ValueError:
        return None


def _matches(finding: FindingLite, label: Label, settings: MatchSettings) -> bool:
    if finding.path != label.path or finding.line is None:
        return False
    if abs(finding.line - label.line) > settings.line_window:
        return False
    if settings.require_category and _category(finding.category) != _category(label.category):
        return False
    return True


def match_case(
    findings: list[FindingLite], labels: list[Label], settings: MatchSettings
) -> CaseMatch:
    result = CaseMatch()
    anchored = [f for f in findings if f.line is not None]
    result.unanchored_findings = [f for f in findings if f.line is None]
    # Candidate findings per label, nearest line first so ties resolve deterministically.
    candidates = [
        sorted(
            (index for index, finding in enumerate(anchored) if _matches(finding, label, settings)),
            key=lambda index: (abs((anchored[index].line or 0) - label.line), index),
        )
        for label in labels
    ]
    owner: dict[int, int] = {}  # finding index -> label index

    def assign(label_index: int, seen: set[int]) -> bool:
        # Kuhn's augmenting path: take a free candidate, or displace an owner that can move.
        for finding_index in candidates[label_index]:
            if finding_index in seen:
                continue
            seen.add(finding_index)
            current = owner.get(finding_index)
            if current is None or assign(current, seen):
                owner[finding_index] = label_index
                return True
        return False

    for label_index in range(len(labels)):
        assign(label_index, set())
    by_label = {label_index: finding_index for finding_index, label_index in owner.items()}
    for label_index, label in enumerate(labels):
        hit = by_label.get(label_index)
        if hit is None:
            result.missed_labels.append(label)
        else:
            result.matched_labels.append(label)
            result.matched_findings.append(anchored[hit])
    result.false_positive_findings = [
        finding for index, finding in enumerate(anchored) if index not in owner
    ]
    return result
