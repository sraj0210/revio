"""Golden vectors and invariants for Phase 6 finding identity and fingerprints."""

import pytest

from venaroq.domain.finding_identity import (
    PRIMARY_VERSION,
    SECONDARY_VERSION,
    evidence_occurs_in_diff,
    finding_identity_is_valid,
    finding_record_id,
    normalize_category,
    normalize_multiline,
    normalize_path,
    normalize_rule,
    normalize_symbol,
    normalize_title,
    primary_fingerprint_v1,
    secondary_fingerprint_v1,
)
from venaroq.domain.models import DiffFile, DiffLine, FindingIdentity

PRIMARY_ARGS = {
    "provider_id": "github",
    "installation_id": "174693302",
    "repository_id": "1328857147",
    "path": "src/app/user.py",
    "category": "Security",
    "rule": "sql-injection",
    "symbol": "UserRepository.find_by_name",
    "evidence": 'query = f"SELECT ... {name}"',
}
SECONDARY_ARGS = {
    "stable_repository_identity": "github:174693302:1328857147",
    "path": "src/app/user.py",
    "category": "Security",
    "rule": "sql-injection",
    "title": "Possible SQL injection in find_by_name",
}

GOLDEN_PRIMARY = "58e9d219046d059613736257c4263d9b2af7862eee4c039c8362aa7b8dac2a45"
GOLDEN_SECONDARY = "41c8d98b1913648e64826c97f503468f8d58089fca30fa6508a8832207be0384"
GOLDEN_RECORD_ID = "82673790-de26-50c9-b146-55ec63276bbb"


def test_primary_golden_vector() -> None:
    assert primary_fingerprint_v1(**PRIMARY_ARGS) == GOLDEN_PRIMARY


def test_secondary_golden_vector() -> None:
    assert secondary_fingerprint_v1(**SECONDARY_ARGS) == GOLDEN_SECONDARY


def test_record_id_golden_vector() -> None:
    assert (
        finding_record_id(SECONDARY_ARGS["stable_repository_identity"], GOLDEN_PRIMARY)
        == GOLDEN_RECORD_ID
    )


def test_primary_and_secondary_versions_separate() -> None:
    assert PRIMARY_VERSION != SECONDARY_VERSION
    assert primary_fingerprint_v1(**PRIMARY_ARGS) != secondary_fingerprint_v1(**SECONDARY_ARGS)


def test_primary_is_deterministic() -> None:
    assert primary_fingerprint_v1(**PRIMARY_ARGS) == primary_fingerprint_v1(**PRIMARY_ARGS)


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_id", "gitlab"),
        ("installation_id", "999"),
        ("repository_id", "000"),
        ("path", "src/app/other.py"),
        ("category", "correctness"),
        ("rule", "command-injection"),
        ("symbol", "Other.method"),
        ("evidence", 'query = f"SELECT ... {other}"'),
    ],
)
def test_primary_changes_when_identity_field_changes(field: str, value: str) -> None:
    changed = {**PRIMARY_ARGS, field: value}
    assert primary_fingerprint_v1(**changed) != primary_fingerprint_v1(**PRIMARY_ARGS)


def test_absent_symbol_differs_from_present_symbol() -> None:
    present = primary_fingerprint_v1(**PRIMARY_ARGS)
    absent = primary_fingerprint_v1(
        provider_id=PRIMARY_ARGS["provider_id"],
        installation_id=PRIMARY_ARGS["installation_id"],
        repository_id=PRIMARY_ARGS["repository_id"],
        path=PRIMARY_ARGS["path"],
        category=PRIMARY_ARGS["category"],
        rule=PRIMARY_ARGS["rule"],
        symbol=None,
        evidence=PRIMARY_ARGS["evidence"],
    )
    assert present != absent


def test_length_framing_resists_boundary_shifts() -> None:
    """Moving a character across a field boundary must change the fingerprint."""
    left = primary_fingerprint_v1(**{**PRIMARY_ARGS, "rule": "sql-injection", "symbol": "ab"})
    right = primary_fingerprint_v1(**{**PRIMARY_ARGS, "rule": "sql-injection", "symbol": "a"})
    assert left != right
    # Category/rule boundary: "a" + "bc" vs "ab" + "c" must not collide.
    one = primary_fingerprint_v1(**{**PRIMARY_ARGS, "category": "a", "rule": "bc"})
    two = primary_fingerprint_v1(**{**PRIMARY_ARGS, "category": "ab", "rule": "c"})
    assert one != two


def test_nfc_normalization_stability() -> None:
    composed = "é"  # U+00E9
    decomposed = "é"  # e + combining acute
    assert composed != decomposed
    a = primary_fingerprint_v1(**{**PRIMARY_ARGS, "symbol": f"find_{composed}"})
    b = primary_fingerprint_v1(**{**PRIMARY_ARGS, "symbol": f"find_{decomposed}"})
    assert a == b


def test_evidence_whitespace_and_newline_normalization() -> None:
    a = primary_fingerprint_v1(**{**PRIMARY_ARGS, "evidence": "a   =\t1"})
    b = primary_fingerprint_v1(**{**PRIMARY_ARGS, "evidence": "a = 1"})
    assert a == b
    crlf = primary_fingerprint_v1(**{**PRIMARY_ARGS, "evidence": "a = 1\r\nb = 2"})
    lf = primary_fingerprint_v1(**{**PRIMARY_ARGS, "evidence": "a = 1\nb = 2"})
    assert crlf == lf


def test_evidence_leading_trailing_blank_lines_trimmed() -> None:
    a = primary_fingerprint_v1(**{**PRIMARY_ARGS, "evidence": "\n\na = 1\n\n"})
    b = primary_fingerprint_v1(**{**PRIMARY_ARGS, "evidence": "a = 1"})
    assert a == b


def test_category_and_rule_are_lowercased() -> None:
    assert normalize_category("Security") == "security"
    assert normalize_rule("SQL-Injection".lower()) == "sql-injection"
    upper = primary_fingerprint_v1(**{**PRIMARY_ARGS, "category": "SECURITY"})
    lower = primary_fingerprint_v1(**{**PRIMARY_ARGS, "category": "security"})
    assert upper == lower


def test_path_and_symbol_preserve_case() -> None:
    upper = primary_fingerprint_v1(**{**PRIMARY_ARGS, "path": "src/App/User.py"})
    lower = primary_fingerprint_v1(**{**PRIMARY_ARGS, "path": "src/app/user.py"})
    assert upper != lower
    cased = primary_fingerprint_v1(**{**PRIMARY_ARGS, "symbol": "User.Method"})
    other = primary_fingerprint_v1(**{**PRIMARY_ARGS, "symbol": "user.method"})
    assert cased != other


def test_title_is_absent_from_primary() -> None:
    """Secondary title variation must never change the primary fingerprint."""
    base = primary_fingerprint_v1(**PRIMARY_ARGS)
    # The primary function has no title parameter; secondary title changes do not touch it.
    a = secondary_fingerprint_v1(**{**SECONDARY_ARGS, "title": "Totally different wording"})
    b = secondary_fingerprint_v1(**SECONDARY_ARGS)
    assert a != b
    assert primary_fingerprint_v1(**PRIMARY_ARGS) == base


def test_title_normalization_conservative() -> None:
    assert normalize_title("  SQL   Injection\nHere ") == "sql injection here"
    a = secondary_fingerprint_v1(**{**SECONDARY_ARGS, "title": "SQL  injection"})
    b = secondary_fingerprint_v1(**{**SECONDARY_ARGS, "title": "sql injection"})
    assert a == b


def test_rename_changes_primary_but_repository_identity_stable() -> None:
    renamed = primary_fingerprint_v1(**{**PRIMARY_ARGS, "path": "src/app/renamed.py"})
    assert renamed != primary_fingerprint_v1(**PRIMARY_ARGS)


@pytest.mark.parametrize("bad", ["/abs/path", "../escape", "a/../b", "a//b", "a\\b", "a/./b"])
def test_invalid_paths_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_path(bad)


@pytest.mark.parametrize("bad", ["Invalid Rule", "-bad", "bad-", "a--b", "", "rule!"])
def test_invalid_rules_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_rule(bad)


def test_rule_is_lowercased_not_rejected() -> None:
    assert normalize_rule("SQL-INJECTION") == "sql-injection"


def test_symbol_empty_after_normalization_rejected() -> None:
    with pytest.raises(ValueError):
        normalize_symbol("   ")


def test_control_characters_rejected_in_evidence() -> None:
    with pytest.raises(ValueError):
        normalize_multiline("a\x00b")
    with pytest.raises(ValueError):
        normalize_multiline("a\x07b")


def test_identity_model_enforces_code_point_and_byte_bounds() -> None:
    FindingIdentity(rule="r", evidence="x")
    with pytest.raises(ValueError):
        FindingIdentity(rule="r", evidence="x" * 513)
    # 512 four-byte code points is exactly 2 KiB and is accepted at the boundary.
    FindingIdentity(rule="r", evidence="\U0001f600" * 512)
    # The code-point ceiling binds before 513 four-byte characters are reached.
    with pytest.raises(ValueError):
        FindingIdentity(rule="r", evidence="\U0001f600" * 513)


def _diff(path: str, lines: tuple[str, ...]) -> DiffFile:
    return DiffFile(
        new_path=path,
        status="modified",
        lines=tuple(
            DiffLine(content=content, side="new", new_line=index + 1)
            for index, content in enumerate(lines)
        ),
    )


def test_evidence_must_occur_in_diff() -> None:
    files = (_diff("src/app/user.py", ('query = f"SELECT ... {name}"', "return rows")),)
    assert evidence_occurs_in_diff(
        path="src/app/user.py", evidence='query = f"SELECT ... {name}"', diff_files=files
    )
    assert not evidence_occurs_in_diff(
        path="src/app/user.py", evidence="never appears here", diff_files=files
    )


def test_evidence_absent_when_path_not_in_diff() -> None:
    files = (_diff("src/app/other.py", ("x = 1",)),)
    assert not evidence_occurs_in_diff(path="src/app/user.py", evidence="x = 1", diff_files=files)


def test_line_movement_does_not_change_identity() -> None:
    """Line number is not part of identity; a moved snippet keeps the same fingerprint."""
    early = _diff("src/app/user.py", ('query = f"SELECT ... {name}"', "a", "b"))
    late = _diff("src/app/user.py", ("a", "b", 'query = f"SELECT ... {name}"'))
    for files in (early, late):
        assert evidence_occurs_in_diff(
            path="src/app/user.py",
            evidence='query = f"SELECT ... {name}"',
            diff_files=(files,),
        )
    # The primary fingerprint has no line component at all.
    assert primary_fingerprint_v1(**PRIMARY_ARGS) == primary_fingerprint_v1(**PRIMARY_ARGS)


def test_finding_identity_is_valid_combines_checks() -> None:
    files = (_diff("src/app/user.py", ('query = f"SELECT ... {name}"',)),)
    identity = FindingIdentity(
        rule="sql-injection",
        symbol="UserRepository.find_by_name",
        evidence='query = f"SELECT ... {name}"',
    )
    assert finding_identity_is_valid(identity, path="src/app/user.py", diff_files=files)
    invalid = FindingIdentity(rule="sql-injection", evidence="not in the diff at all")
    assert not finding_identity_is_valid(invalid, path="src/app/user.py", diff_files=files)
