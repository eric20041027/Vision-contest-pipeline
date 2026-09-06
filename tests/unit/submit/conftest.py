import pytest

from submit_fixtures import make_pair


@pytest.fixture
def pair(roots, tmp_path):
    return make_pair(roots, tmp_path)
