"""Allow ``python -m vcp`` wherever the ``vcp`` console script is not on PATH.

Same app, same VERDICT lines and exit codes as the ``vcp`` entry point.
"""

from vcp.cli import app

if __name__ == "__main__":
    app()
