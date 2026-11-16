"""package_test.py — the installed distribution and the package agree on one version."""

from importlib import metadata

import tart_taste_test


def test_distribution_version_is_the_package_version() -> None:
    assert metadata.version("tart-taste-test") == tart_taste_test.__version__
