"""The public API instance packages import."""

from __future__ import annotations

import pytest

from second_brain import ext


def test_every_public_name_is_there() -> None:
    for name in ext.__all__:
        assert hasattr(ext, name), name


def test_the_matching_version_is_accepted() -> None:
    ext.require_api(ext.API_VERSION)


def test_another_version_is_refused() -> None:
    with pytest.raises(ext.ExtensionError, match="version"):
        ext.require_api(ext.API_VERSION + 1)
