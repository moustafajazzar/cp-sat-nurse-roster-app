import pytest

from hospital.catalogue import seed_catalogue


@pytest.fixture
def catalogue(db):
    """What `bootstrap` seeds on first start."""
    seed_catalogue()
