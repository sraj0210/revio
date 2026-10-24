"""Deterministic ``diff-hash-v1`` vectors and sensitivity checks."""

from revio.domain.diff_hash import diff_hash_v1
from revio.domain.models import DiffFile, DiffLine

GOLDEN = "a7568b08275d79395e63fa9834246041240547ee58a793aacba48f88848f11be"


def _file(**overrides: object) -> DiffFile:
    base: dict[str, object] = {
        "new_path": "a.py",
        "status": "modified",
        "lines": (
            DiffLine(content="x = 1", side="new", new_line=1),
            DiffLine(content="y = 2", side="new", new_line=2),
        ),
    }
    base.update(overrides)
    return DiffFile(**base)  # type: ignore[arg-type]


def test_golden_vector() -> None:
    assert diff_hash_v1((_file(),)) == GOLDEN


def test_deterministic() -> None:
    assert diff_hash_v1((_file(),)) == diff_hash_v1((_file(),))


def test_file_order_changes_hash() -> None:
    first = _file(new_path="a.py")
    second = _file(new_path="b.py")
    assert diff_hash_v1((first, second)) != diff_hash_v1((second, first))


def test_content_change_changes_hash() -> None:
    changed = _file(
        lines=(
            DiffLine(content="x = 1", side="new", new_line=1),
            DiffLine(content="y = 3", side="new", new_line=2),
        )
    )
    assert diff_hash_v1((changed,)) != GOLDEN


def test_status_change_changes_hash() -> None:
    assert diff_hash_v1((_file(status="added"),)) != GOLDEN


def test_side_change_changes_hash() -> None:
    changed = _file(
        lines=(
            DiffLine(content="x = 1", side="context", new_line=1),
            DiffLine(content="y = 2", side="new", new_line=2),
        )
    )
    assert diff_hash_v1((changed,)) != GOLDEN


def test_path_change_changes_hash() -> None:
    assert diff_hash_v1((_file(new_path="b.py"),)) != GOLDEN


def test_patch_state_change_changes_hash() -> None:
    assert diff_hash_v1((_file(patch_state="provider_truncated"),)) != GOLDEN


def test_crlf_content_normalized() -> None:
    crlf = _file(lines=(DiffLine(content="a\r\nb", side="new", new_line=1),))
    lf = _file(lines=(DiffLine(content="a\nb", side="new", new_line=1),))
    assert diff_hash_v1((crlf,)) == diff_hash_v1((lf,))
