import pytest

from vcp.code import rules
from vcp.code.rules import file_class, is_secret, skipped_segment


def test_limits_and_prefix_are_the_specs():
    assert (rules.KEEP_FILE_LIMIT, rules.KEEP_ROOT_LIMIT) == (1024**2, 64 * 1024**2)
    assert (rules.MAX_FILES, rules.MAX_HASH_BYTES, rules.MAX_WALK_ENTRIES) == (
        20_000,
        512 * 1024**2,
        200_000,
    )
    assert (rules.LISTED_PATHS, rules.IDENTITY_PREFIX, rules.RULES_VERSION) == (
        50,
        "vcp-code/1\n",
        1,
    )


def test_the_two_suffix_groups_are_the_specs():
    assert rules.CODE_SUFFIXES == {
        ".py",
        ".pyi",
        ".pyx",
        ".pxd",
        ".c",
        ".cc",
        ".cpp",
        ".cu",
        ".cuh",
        ".h",
        ".hpp",
    }
    assert rules.CONFIG_SUFFIXES == {
        ".pyd",
        ".so",
        ".ipynb",
        ".sh",
        ".ps1",
        ".bat",
        ".cmd",
        ".toml",
        ".cfg",
        ".ini",
        ".yaml",
        ".yml",
        ".json",
    }
    assert rules.CODE_SUFFIXES.isdisjoint(rules.CONFIG_SUFFIXES)
    assert rules.CONFIG_GLOBS == (
        "requirements*.txt",
        "constraints*.txt",
        "*.lock",
        "Makefile",
        "Dockerfile",
    )


@pytest.mark.parametrize(
    ("name", "cls"),
    [
        ("a.PY", "code"),
        ("k.cu", "code"),
        ("m.HPP", "code"),
        ("x.Json", "config"),
        ("nb.ipynb", "config"),
        ("ext.pyd", "config"),
        ("requirements-dev.txt", "config"),
        ("Requirements.TXT", "config"),
        ("constraints.txt", "config"),
        ("uv.lock", "config"),
        ("Makefile", "config"),
        ("makefile", "config"),
        ("Dockerfile", "config"),
        ("README.md", None),
        ("notes.txt", None),
        ("d.csv", None),
        ("i.png", None),
        ("noextension", None),
        ("", None),
    ],
)
def test_two_groups_ignoring_case(name, cls):
    assert file_class(name) == cls


@pytest.mark.parametrize(
    "name",
    [
        "server.pem",
        "a.KEY",
        "id_rsa.pub",
        "id_ed25519",
        "id_ecdsa",
        "Kaggle.JSON",
        "rclone.conf",
        "pg_service.conf",
        "pgpass.conf",
        "_netrc",
        "credentials.yaml",
        "my_secret_cfg.py",
        "gcp-service-account.json",
        "x_service_account.py",
        "cert.p12",
        "store.PFX",
        "app.jks",
        "vault.kdbx",
    ],
)
def test_secret_names(name):
    assert is_secret(name)


def test_ordinary_names_are_not_secret():
    assert not any(map(is_secret, ["train.py", "config.yaml", "keys.py", "key.py", "service.py"]))


@pytest.mark.parametrize(
    "segment",
    [
        ".git",
        ".venv",
        ".env.local",
        ".claude",
        ".hydra",
        "__pycache__",
        "__PYCACHE__",
        "node_modules",
        "Site-Packages",
        "pkg.egg-info",
        "PKG.EGG-INFO",
    ],
)
def test_skipped_segments(segment):
    assert skipped_segment(segment)


@pytest.mark.parametrize(
    "segment", ["src", "train.py", "egg_info", "my.egg-info.py", "site_packages"]
)
def test_ordinary_segments_are_not_skipped(segment):
    assert not skipped_segment(segment)
