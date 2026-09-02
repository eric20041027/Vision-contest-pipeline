import vcp


def test_version_is_non_empty_string():
    assert isinstance(vcp.__version__, str)
    assert vcp.__version__ == "0.1.0"
