"""Package import tests."""

import venaroq


def test_package_is_importable() -> None:
    assert venaroq.__version__ == "0.1.0"
