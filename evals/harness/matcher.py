"""Deterministic finding<->label matching.

A finding matches a labeled defect when it is on the same file and its line falls within a window of
the label's line (optionally also requiring category agreement). Matching is greedy and one-to-one:
each finding and each label is used at most once. Findings without a line ("unanchored" summary
findings) cannot match a line label; they are reported separately and are neither credited nor
counted as false positives, so general summary prose does not distort precision.
"""

from dataclasses import dataclass, field

from evals.harness.cases import FindingLite, Label


@dataclass(frozen=True)
class MatchSettings:
    line_window: int = 3
    require_category: bool = False


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


def _matches(finding: FindingLite, label: Label, settings: MatchSettings) -> bool:
    if finding.path != label.path or finding.line is None:
        return False
    if abs(finding.line - label.line) > settings.line_window:
        return False
    if settings.require_category and finding.category != label.category:
        return False
    return True


def match_case(
    findings: list[FindingLite], labels: list[Label], settings: MatchSettings
) -> CaseMatch:
    result = CaseMatch()
    anchored = [f for f in findings if f.line is not None]
    result.unanchored_findings = [f for f in findings if f.line is None]
    used: set[int] = set()
    for label in labels:
        hit: int | None = None
        for index, finding in enumerate(anchored):
            if index in used:
                continue
            if _matches(finding, label, settings):
                hit = index
                break
        if hit is None:
            result.missed_labels.append(label)
        else:
            used.add(hit)
            result.matched_labels.append(label)
            result.matched_findings.append(anchored[hit])
    result.false_positive_findings = [
        finding for index, finding in enumerate(anchored) if index not in used
    ]
    return result
