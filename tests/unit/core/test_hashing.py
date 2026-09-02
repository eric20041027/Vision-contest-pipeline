from vcp.core import hashing as h


def test_canonical_json_is_order_independent():
    assert h.canonical_json({"b": 1, "a": [1, 2]}) == h.canonical_json({"a": [1, 2], "b": 1})
    assert h.canonical_json({"a": 1}) == '{"a":1}'
    assert h.sha256_json({"x": "中"}) == h.sha256_text('{"x":"中"}')


def test_file_digests(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello")
    assert h.sha256_file(p) == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert h.md5_file(p) == "5d41402abc4b2a76b9719d911017c592"


def test_dir_manifest_sorted_posix_and_hash_stable(tmp_path):
    root = tmp_path / "raw"
    (root / "b").mkdir(parents=True)
    (root / "b" / "y.txt").write_bytes(b"y")
    (root / "a.txt").write_bytes(b"a")
    lines = h.dir_manifest(root)
    assert lines == [
        f"a.txt\t1\t{h.md5_file(root / 'a.txt')}",
        f"b/y.txt\t1\t{h.md5_file(root / 'b' / 'y.txt')}",
    ]
    out = tmp_path / "manifest.txt"
    digest = h.write_manifest(lines, out)
    assert digest == h.manifest_hash(lines) == h.sha256_file(out)
    assert b"\r\n" not in out.read_bytes()


def test_dir_manifest_empty_dir(tmp_path):
    assert h.dir_manifest(tmp_path) == []
    assert h.manifest_hash([]) == h.sha256_text("\n")
