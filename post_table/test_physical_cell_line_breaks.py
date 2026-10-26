"""公共换行恢复的物理归属与跨页回归测试，不调用解析器或模型。"""

import post_table.restore_cell_line_breaks as m


def run(text, x=10, y=10):
    return m.TextRun(text, x, x + 10, y, y + 8, int(y))


def table(rows):
    return "<table>" + "".join(
        "<tr>" + "".join("<td>" + v + "</td>" for v in row) + "</tr>"
        for row in rows
    ) + "</table>"


def source(page, rows, bbox=(0, 0, 100, 100)):
    return m.PhysicalTable(page, (100, 100), bbox, table(rows))


def test_unique_diagram_digits_cannot_modify_unlocated_cell():
    html = table([["23"]])
    diagram = [[run("2")], [run("3", y=20)]]
    assert m._restore_table_cells(
        html, diagram, fallback_runs_by_page=[diagram],
        runs_by_column=[diagram], logical_cells=[(0, 1)],
    ) == (html, 0, 0)


def test_empty_cell_evidence_does_not_fall_back_to_diagram():
    html = table([["23"]])
    diagram = [[run("2")], [run("3", y=20)]]
    assert m._restore_table_cells(
        html, diagram, runs_by_cell=[[]], fallback_runs_by_page=[diagram],
    ) == (html, 0, 0)


def test_real_multiline_digits_inside_same_cell_are_preserved_as_multiline():
    html = table([["36"]])
    evidence = [[run("3")], [run("6", y=20)]]
    assert m._restore_table_cells(html, [], runs_by_cell=[evidence]) == (
        table([["3<br>6"]]), 1, 1,
    )


def test_complete_cell_number_and_name_are_not_split():
    html = table([["16", "STR1_SRC"]])
    assert m._restore_table_cells(
        html, [], runs_by_cell=[[[run("16")]], [[run("STR1_SRC")]]],
    ) == (html, 0, 0)


def test_far_apart_fragments_never_form_wide_run():
    variants = m._line_run_variants([run("_", 0), run("9", 400)])
    assert len(variants) == 2


def test_degenerate_or_distant_lines_are_not_multiline_evidence():
    zero = m.TextRun("2", 10, 10, 10, 10, 0)
    assert not m._same_cell_column(zero, run("1", y=20))
    assert not m._same_cell_column(run("2"), run("1", y=100))


def test_source_snapshot_uses_premerge_tables_and_keeps_own_page():
    original = table([["Pin"], ["23"]])
    span = {"html": original, "bbox": [1, 2, 90, 95]}
    pages = [{"page_idx": 4, "page_size": [100, 100],
              "preproc_blocks": [span],
              "para_blocks": [{"html": table([["wrong"]]), "bbox": [0, 0, 1, 1]}]}]
    sources = m._physical_table_sources(pages)
    span["html"] = "changed"
    assert sources == [m.PhysicalTable(4, (100, 100), (1, 2, 90, 95), original)]


def test_merged_rows_map_beyond_two_pages_using_complete_rows(monkeypatch):
    rows = [["Pin", "Name"], ["1", "A"], ["2", "B"],
            ["3", "C"], ["4", "D1D2"]]
    sources = [source(0, rows[:3]), source(5, [rows[0], *rows[3:]])]
    calls = []

    def evidence(s, doc):
        calls.append((s.page_index, s.bbox))
        keys = m._row_cell_keys(s.html)
        result = []
        for _, cells in keys:
            for *_, text in cells:
                result.append([[run(text)]] if text != "D1D2" else
                              [[run("D1")], [run("D2", y=20)]])
        return result

    monkeypatch.setattr(m, "_source_cell_runs", evidence)
    html = table(rows)
    runs = m._map_physical_cell_runs(
        html, {"page_idx": 0}, [0, 0, 100, 100], sources, None, {},
    )
    corrected, cells, breaks = m._restore_table_cells(html, [], runs_by_cell=runs)
    assert corrected == html.replace("D1D2", "D1<br>D2")
    assert (cells, breaks) == (1, 1)
    assert calls == [(0, (0, 0, 100, 100)), (5, (0, 0, 100, 100))]


def test_repeated_rows_and_duplicate_sources_remain_unchanged(monkeypatch):
    rows = [["Pin"], ["12"], ["23"]]
    monkeypatch.setattr(m, "_source_cell_runs", lambda s, doc: [
        [], [[run("1")], [run("2", y=20)]],
        [[run("2")], [run("3", y=20)]],
    ])
    html = table(rows)
    runs = m._map_physical_cell_runs(
        html, {"page_idx": 0}, [0, 0, 1, 1],
        [source(1, rows), source(2, rows)], None, {},
    )
    assert m._restore_table_cells(html, [], runs_by_cell=runs) == (html, 0, 0)
    repeated = table([*rows, rows[1]])
    runs = m._map_physical_cell_runs(
        repeated, {"page_idx": 0}, [0, 0, 1, 1], [source(1, rows)], None, {},
    )
    assert m._restore_table_cells(repeated, [], runs_by_cell=runs) == (repeated, 0, 0)


def test_row_identity_keeps_underscores_and_column_positions():
    assert m._row_cell_keys(table([["1", "AB"]])) != m._row_cell_keys(table([["1", "A_B"]]))
    assert m._row_cell_keys(table([["1", "A"]])) != m._row_cell_keys(table([["A", "1"]]))


def test_grid_failure_leaves_original_unchanged(monkeypatch):
    rows = [["Pin"], ["12"], ["23"]]
    monkeypatch.setattr(m, "_source_cell_runs", lambda s, doc: None)
    html = table(rows)
    runs = m._map_physical_cell_runs(
        html, {"page_idx": 0}, [0, 0, 100, 100], [source(0, rows)], None, {},
    )
    assert m._restore_table_cells(html, [], runs_by_cell=runs) == (html, 0, 0)


def test_existing_explicit_line_break_is_not_rewritten():
    html = table([["3<br>6"]])
    assert m._restore_table_cells(html, [], runs_by_cell=[[[run("36")]]]) == (html, 0, 0)


def test_physical_page_and_bbox_are_used_together(monkeypatch):
    calls = []

    class TextPage:
        def close(self):
            calls.append("text_closed")

    class Page:
        def get_size(self):
            return 1000, 800

        def get_textpage(self):
            return TextPage()

    class Document:
        def __len__(self):
            return 8

        def __getitem__(self, index):
            calls.append(("page", index))
            return Page()

    def boundaries(page, bbox, height, segments):
        calls.append(("grid", tuple(bbox), height, segments))
        return [100, 500] if segments == 1 else [200, 400, 600]

    def characters(page, bbox, height):
        calls.append(("characters", tuple(bbox)))
        return []

    monkeypatch.setattr(m, "_detect_table_column_boundaries", boundaries)
    monkeypatch.setattr(m, "_detect_table_row_boundaries", boundaries)
    monkeypatch.setattr(m, "_extract_pdf_characters", characters)
    s = m.PhysicalTable(5, (2000, 1600), (200, 400, 1000, 1200), table([["H"], ["23"]]))
    assert m._source_cell_runs(s, Document()) == [[], []]
    assert calls == [("page", 5), ("grid", (100, 200, 500, 600), 800, 1),
                     ("grid", (100, 200, 500, 600), 800, 2),
                     ("characters", (100, 200, 500, 600)), "text_closed"]


def test_unlocatable_duplicate_source_is_still_ambiguous(monkeypatch):
    rows = [["Pin"], ["12"], ["23"]]
    def evidence(s, doc):
        return None if s.page_index == 2 else [
            [], [[run("1")], [run("2", y=20)]],
            [[run("2")], [run("3", y=20)]],
        ]
    monkeypatch.setattr(m, "_source_cell_runs", evidence)
    html = table(rows)
    runs = m._map_physical_cell_runs(html, {"page_idx": 0}, [0, 0, 1, 1],
                                     [source(1, rows), source(2, rows)], None, {})
    assert m._restore_table_cells(html, [], runs_by_cell=runs) == (html, 0, 0)


def test_noncontiguous_rows_cannot_claim_a_continuation(monkeypatch):
    rows = [["Pin"], ["1"], ["2"], ["3"]]
    def forbidden(s, doc):
        raise AssertionError("不连续的物理表不应进入坐标处理")
    monkeypatch.setattr(m, "_source_cell_runs", forbidden)
    html = table(rows)
    runs = m._map_physical_cell_runs(html, {"page_idx": 0}, [0, 0, 1, 1],
                                     [source(1, [rows[0], rows[1], rows[3]])], None, {})
    assert m._restore_table_cells(html, [], runs_by_cell=runs) == (html, 0, 0)
