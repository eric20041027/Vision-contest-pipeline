from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

from graph_fixtures import (
    CACHE,
    DS,
    DS2,
    FUSED,
    JUDGE,
    PERFECT,
    SPLIT,
    SUB,
    add_entity,
    contest_graph,
    status,
)
from vcp.core.errors import ValidationFailed
from vcp.provenance.graph import ProvenanceGraph
from vcp.provenance.render import build_view, node_id
from vcp.provenance.render_mermaid import (
    MARKER,
    MERMAID_SRI,
    MERMAID_VERSION,
    DocumentMeta,
    prepare_output,
    render_document,
    to_mermaid,
    write_document,
)
from vcp.provenance.schema import EntityStatus

META = DocumentMeta(
    build="0.9.0+gabc",
    backend="sqlite",
    index_hash="0123456789ab",
    head=None,
    generated="2026-09-23T00:00:00.000Z",
)
KIND = node_id("kind:contest-note")


def _contest_text() -> str:
    statuses = {PERFECT: status(PERFECT, EntityStatus.STALE)}
    return to_mermaid(build_view(contest_graph(), statuses))


def test_mermaid_draws_each_kind_with_its_own_shape():
    text = _contest_text()

    assert text.splitlines()[0] == "flowchart LR"
    assert f'{node_id(DS)}[("tiny<br>@aaaa1111 - 1,200 samples")]' in text
    assert f'{node_id(SPLIT)}[/"fixed-v1<br>train"/]' in text
    assert f'{node_id(PERFECT)}["perfect<br>grade receipt"]' in text
    assert f'{node_id(FUSED)}{{{{"fused<br>fusion mean-v1"}}}}' in text
    assert f'{node_id(JUDGE)}>"admit-perfect<br>PASS"]' in text
    assert f'{node_id(SUB)}(["s1<br>candidate"])' in text
    assert f'{KIND}[["contest-note x2<br>1 BROKEN"]]' in text


def test_mermaid_frames_datasets_and_labels_edges():
    text = _contest_text()

    assert re.search(r'^  subgraph g[0-9a-f]{12}\["tiny"\]\n    direction LR$', text, re.MULTILINE)
    assert re.search(r'^  subgraph g[0-9a-f]{12}\["tiny-test"\]$', text, re.MULTILINE)
    assert f'  {node_id(DS)} ==>|"diff d1: 3 changes"| {node_id(DS2)}' in text
    assert f'  {node_id(PERFECT)} -->|"valA, valB"| {node_id(JUDGE)}' in text
    assert f"  {node_id(DS)} --> {node_id(SPLIT)}" in text


def test_mermaid_styles_status_and_backup_with_classes():
    text = _contest_text()

    assert "classDef st_broken " in text and "classDef st_stale " in text
    assert f"  class {KIND} st_broken" in text
    assert f"  class {node_id(PERFECT)} st_stale" in text
    assert re.search(rf"^  class [^ ]*{node_id(PERFECT)}[^ ]* backed$", text, re.MULTILINE)


def test_mermaid_output_is_deterministic():
    assert _contest_text() == _contest_text()


def test_labels_are_escaped_for_mermaid():
    graph = ProvenanceGraph()
    add_entity(graph, 'run:evil"<b>#1|x`y', dataset="tiny", provenance_grade="declared")

    text = to_mermaid(build_view(graph, {}))

    assert "<b>" not in text
    assert "evil#quot;#lt;b#gt;#35;1#124;x#96;y<br>grade declared" in text


def test_md_document_carries_marker_meta_legend_and_hidden_problems():
    statuses = {CACHE: status(CACHE, EntityStatus.BROKEN, "materialized output mismatch")}
    view = build_view(contest_graph(), statuses)

    doc = render_document(view, "md", META)

    assert MARKER in doc.text.splitlines()[0]
    assert "```mermaid\nflowchart LR\n" in doc.text
    assert "`0123456789ab`" in doc.text and "`0.9.0+gabc`" in doc.text
    assert "materialized_cache:tiny/npy@m1" in doc.text
    assert "materialized output mismatch" in doc.text
    assert "| BROKEN |" in doc.text
    assert doc.oversize is False


def test_mmd_document_is_plain_mermaid_behind_a_comment_header():
    doc = render_document(build_view(contest_graph(), {}), "mmd", META)
    lines = doc.text.splitlines()

    assert lines[0].startswith("%% ") and MARKER in lines[0]
    assert "flowchart LR" in lines
    assert all(line.startswith("%%") for line in lines[: lines.index("flowchart LR")])


def test_html_document_pins_mermaid_and_escapes_everything_it_embeds():
    graph = contest_graph()
    add_entity(graph, "run:</pre><script>alert(1)</script>", dataset="tiny")

    doc = render_document(build_view(graph, {}), "html", META)

    assert MARKER in doc.text[:4096]
    assert f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}/dist/mermaid.min.js" in doc.text
    assert f'integrity="{MERMAID_SRI}"' in doc.text
    assert 'crossorigin="anonymous"' in doc.text
    assert "<script>alert(1)" not in doc.text
    assert '<pre class="mermaid">flowchart LR\n' in doc.text
    assert "&lt;br&gt;" in doc.text  # label breaks reach Mermaid as text, not as markup
    assert 'securityLevel: "strict"' in doc.text


def _fan(width: int):
    graph = ProvenanceGraph()
    add_entity(graph, "dataset:wide@ffff", dsv="dataset:wide@ffff", dataset="wide")
    for index in range(width):
        run = f"run:r{index:04d}"
        add_entity(graph, run, dsv="dataset:wide@ffff", dataset="wide")
        graph.add_edge("dataset:wide@ffff", run, "CONSUMED_BY")
    return build_view(graph, {})


def test_oversize_follows_mermaid_default_limits_except_in_html():
    small, wide = _fan(500), _fan(501)

    assert render_document(small, "md", META).oversize is False
    assert render_document(wide, "md", META).oversize is True
    assert render_document(wide, "mmd", META).oversize is True
    assert render_document(wide, "html", META).oversize is False


@pytest.mark.parametrize(("name", "fmt"), [("g.html", "html"), ("g.MD", "md"), ("g.mmd", "mmd")])
def test_prepare_output_takes_the_format_from_the_suffix(tmp_path, name, fmt):
    target, chosen = prepare_output(tmp_path / "views" / name, tmp_path / "data")

    assert chosen == fmt
    assert target == (tmp_path / "views" / name).resolve()


def test_prepare_output_refuses_unknown_suffixes_and_the_data_root(tmp_path):
    with pytest.raises(ValidationFailed, match="unsupported_format"):
        prepare_output(tmp_path / "graph.svg", tmp_path / "data")
    with pytest.raises(ValidationFailed, match="out_in_data_root"):
        prepare_output(tmp_path / "data" / "views" / "graph.html", tmp_path / "data")


def test_prepare_output_only_replaces_files_it_wrote(tmp_path):
    foreign = tmp_path / "notes.md"
    foreign.write_text("my notes\n", encoding="utf-8")
    ours = tmp_path / "graph.md"
    write_document(ours, render_document(build_view(contest_graph(), {}), "md", META).text)
    folder = tmp_path / "folder.html"
    folder.mkdir()

    with pytest.raises(ValidationFailed, match="exists"):
        prepare_output(foreign, tmp_path / "data")
    with pytest.raises(ValidationFailed, match="not_a_file"):
        prepare_output(folder, tmp_path / "data")
    assert prepare_output(ours, tmp_path / "data")[1] == "md"
    assert foreign.read_text(encoding="utf-8") == "my notes\n"


def test_a_file_that_merely_quotes_the_marker_is_not_ours(tmp_path):
    notes = tmp_path / "cli.md"
    notes.write_text(f"# Commands\n\nOld drawings carry `{MARKER}` at the top.\n", encoding="utf-8")

    with pytest.raises(ValidationFailed, match="exists"):
        prepare_output(notes, tmp_path / "data")


@pytest.mark.parametrize("fmt", ["html", "md", "mmd"])
def test_our_file_stays_ours_after_crlf_and_bom_rewrites(tmp_path, fmt):
    text = render_document(build_view(contest_graph(), {}), fmt, META).text
    target = tmp_path / f"graph.{fmt}"
    target.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))

    assert prepare_output(target, tmp_path / "data") == (target.resolve(), fmt)


def test_write_document_replaces_in_place_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "nested" / "graph.mmd"

    write_document(target, "first\n")
    write_document(target, "second\n")

    assert target.read_bytes() == b"second\n"
    assert sorted(path.name for path in target.parent.iterdir()) == ["graph.mmd"]


def test_long_lists_of_hidden_problems_are_cut_with_a_count():
    graph = contest_graph()
    statuses = {}
    for index in range(53):
        cache = f"materialized_cache:tiny/npy-{index:02d}@m"
        add_entity(graph, cache, dsv=DS, dataset="tiny", mode="npy", rows=1)
        statuses[cache] = status(cache, EntityStatus.BROKEN, "materialized output mismatch")
    view = build_view(graph, statuses)

    md = render_document(view, "md", META).text
    mmd = render_document(view, "mmd", META).text

    assert "… and 3 more (`--json` lists every one)." in md
    assert "%% not drawn: 3 more (--json lists every one)" in mmd
    assert len(view.hidden_alerts) == 53


def test_an_unknown_format_is_a_validation_failure():
    with pytest.raises(ValidationFailed, match="unsupported_format"):
        render_document(build_view(contest_graph(), {}), "svg", META)


def test_a_path_on_another_drive_is_outside_the_data_root(tmp_path, monkeypatch):
    def different_drives(paths):
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr("vcp.provenance.render_mermaid.os.path.commonpath", different_drives)

    assert prepare_output(tmp_path / "graph.html", tmp_path / "data")[1] == "html"


def test_a_failed_replace_keeps_the_old_file_and_leaves_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "graph.md"
    write_document(target, "old\n")

    def locked(source, destination):
        raise PermissionError("file is locked")

    monkeypatch.setattr("vcp.provenance.render_mermaid.os.replace", locked)

    with pytest.raises(PermissionError):
        write_document(target, "new\n")
    assert target.read_bytes() == b"old\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["graph.md"]


def test_the_data_root_is_recognised_by_identity_when_spelling_differs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "artifacts").mkdir(parents=True)

    def never_common(paths):
        raise ValueError("spelled differently")

    monkeypatch.setattr("vcp.provenance.render_mermaid.os.path.commonpath", never_common)

    with pytest.raises(ValidationFailed, match="out_in_data_root"):
        prepare_output(data / "artifacts" / "graph.html", data)


@pytest.mark.skipif(sys.platform != "win32", reason="drive-relative paths exist only on Windows")
def test_a_path_that_resolves_drive_relative_is_refused(tmp_path):
    mistyped = Path(chr(92) + "?" + chr(92) + str(tmp_path / "graph.html"))  # \?\C:... typo

    with pytest.raises(ValidationFailed, match="out_not_absolute"):
        prepare_output(mistyped, tmp_path / "data")


def test_percent_signs_cannot_open_a_mermaid_directive():
    graph = ProvenanceGraph()
    add_entity(graph, "run:%%{init: {'theme': 'forest'}}%%", dataset="tiny")
    add_entity(graph, "materialized_cache:tiny/x@m", dataset="tiny")
    statuses = {
        "materialized_cache:tiny/x@m": status(
            "materialized_cache:tiny/x@m", EntityStatus.BROKEN, "%%{init: {'theme': 'dark'}}%%"
        )
    }
    view = build_view(graph, statuses)
    markdown = render_document(view, "md", META).text

    # Only text Mermaid parses matters: the diagram (also inside the html <pre>), every .mmd
    # line, and the fenced block of the .md; the .md / .html tables are never parsed by it.
    assert "%%{" not in to_mermaid(view)
    assert "%%{" not in render_document(view, "mmd", META).text
    assert "%%{" not in markdown.split("```mermaid", 1)[1].split("```", 1)[0]


def test_a_volume_without_file_ids_does_not_make_everything_the_data_root(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    real_stat = os.stat

    def no_ids(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        return os.stat_result((result.st_mode, 0, 7, *tuple(result)[3:]))  # st_ino 0, one st_dev

    def never_common(paths):
        raise ValueError("spelled differently")

    monkeypatch.setattr("vcp.provenance.render_mermaid.os.stat", no_ids)
    monkeypatch.setattr("vcp.provenance.render_mermaid.os.path.commonpath", never_common)

    assert prepare_output(tmp_path / "views" / "graph.html", data)[1] == "html"
