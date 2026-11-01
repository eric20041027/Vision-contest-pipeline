import logging

import pytest


@pytest.fixture
def vcp_log(caplog, monkeypatch):
    """``setup_logging`` (any CLI test before this one) turns propagation off on the ``vcp``
    logger; turn it back on so caplog sees the records."""
    monkeypatch.setattr(logging.getLogger("vcp"), "propagate", True)
    caplog.set_level(logging.DEBUG, logger="vcp")
    return caplog
