"""Package import tests."""

import revio


def test_package_is_importable() -> None:
    assert revio.__version__ == "0.1.0"
