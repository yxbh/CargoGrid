import pytest
from build123d import Box

from cargo_grid import catalogue


@pytest.fixture
def accessory_metadata_shape(monkeypatch):
    """Exercise design metadata without constructing an unrelated family body."""
    monkeypatch.setattr(catalogue, "make_accessory", lambda spec: Box(2, 3, 4))
