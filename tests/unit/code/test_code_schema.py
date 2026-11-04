import subprocess
import sys
from typing import get_args

import pytest

from vcp.code import schema
from vcp.code.scan import RootKind as ScanRootKind
from vcp.code.scan import ScanReason
from vcp.code.snapshot import WriteReason

# R8: the modules `vcp.train.schema` / `vcp.train.env` import must never import the training layer.
_LOW_LAYER = ["schema", "rules", "scan", "roots", "generated"]


@pytest.mark.parametrize("module", _LOW_LAYER)
def test_the_low_code_layer_never_imports_the_training_layer(module):
    src = (
        "import sys\n"
        f"import vcp.code.{module}\n"
        "bad = sorted(m for m in sys.modules if m == 'vcp.train' or m.startswith('vcp.train.'))\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", src], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_schema_imports_nothing_of_vcp():
    src = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import vcp.code.schema\n"
        "new = sorted(m for m in set(sys.modules) - before if m.startswith('vcp.'))\n"
        "print(new)\n"
        "sys.exit(0 if new == ['vcp.code', 'vcp.code.schema'] else 1)\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", src], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_root_reason_is_a_scan_reason_or_a_write_reason():
    assert set(get_args(schema.RootReason)) == set(get_args(ScanReason)) | set(
        get_args(WriteReason)
    )


def test_the_root_kind_is_the_one_scan_uses():
    assert ScanRootKind is schema.RootKind
    assert set(get_args(schema.RootKind)) == {"git", "dir"}


def test_code_grades_rank_from_declared_up_to_traced():
    assert get_args(schema.CodeGrade) == ("declared", "commit", "snapshot", "traced")
    assert schema.CODE_RANK == {"declared": 0, "commit": 1, "snapshot": 2, "traced": 3}
    assert sorted(schema.CODE_RANK, key=schema.CODE_RANK.__getitem__) == list(
        get_args(schema.CodeGrade)
    )
