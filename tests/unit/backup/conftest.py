import pytest

from backup_fixtures import make_world


@pytest.fixture
def world(roots, tmp_path):
    return make_world(roots, tmp_path)
