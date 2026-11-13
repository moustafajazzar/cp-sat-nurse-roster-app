import pytest

from hospital.catalogue import seed_catalogue


@pytest.fixture(autouse=True)
def catalogue(db):
    """What `bootstrap` seeds on first start: a unit's defaults name its shifts."""
    seed_catalogue()
