from post_table.restore_cell_line_breaks import (
    LogicalCell,
    TextRun,
    _detect_table_column_boundaries,
    _detect_table_row_boundaries,
    _logical_cell_layout,
    _logical_cell_columns,
    _restore_table_cells,
    _scale_bbox_to_pdf,
)


def _run(text: str, x0: float, x1: float, line_index: int) -> TextRun:
    return TextRun(
        text=text,
        x0=x0,
        x1=x1,
        y0=float(line_index * 10),
        y1=float(line_index * 10 + 8),
        line_index=line_index,
    )


def test_each_cell_restores_its_own_visual_lines_independently():
    html = (
        "<table><tr>"
        "<td>A1A2A3</td>"
        "<td>GND</td>"
        "<td>First lineSecond line</td>"
        "</tr></table>"
    )
    runs_by_line = [
        [
            _run("A1", 0, 12, 0),
            _run("GND", 100, 125, 0),
            _run("First line", 200, 260, 0),
        ],
        [
            _run("A2", 0, 12, 1),
            _run("Second line", 200, 270, 1),
        ],
        [_run("A3", 0, 12, 2)],
    ]

    corrected, changed_cells, added_breaks = _restore_table_cells(
        html,
        runs_by_line,
    )

    assert "<td>A1<br>A2<br>A3</td>" in corrected
    assert "<td>GND</td>" in corrected
    assert "<td>First line<br>Second line</td>" in corrected
    assert changed_cells == 2
    assert added_breaks == 3


def test_short_cell_is_not_excluded_by_arbitrary_length_limit():
    html = "<table><tr><td>A1A2</td></tr></table>"
    runs_by_line = [
        [_run("A1", 0, 12, 0)],
        [_run("A2", 0, 12, 1)],
    ]

    corrected, changed_cells, added_breaks = _restore_table_cells(
        html,
        runs_by_line,
    )

    assert corrected == "<table><tr><td>A1<br>A2</td></tr></table>"
    assert changed_cells == 1
    assert added_breaks == 1


def test_unmatched_cell_remains_unchanged():
    html = "<table><tr><td>A1A2</td></tr></table>"
    runs_by_line = [
        [_run("A1", 0, 12, 0)],
        [_run("B2", 0, 12, 1)],
    ]

    corrected, changed_cells, added_breaks = _restore_table_cells(
        html,
        runs_by_line,
    )

    assert corrected == html
    assert changed_cells == 0
    assert added_breaks == 0


def test_middle_json_bbox_is_scaled_to_pdf_points():
    assert _scale_bbox_to_pdf(
        [100.0, 200.0, 900.0, 1400.0],
        source_width=1200.0,
        source_height=1600.0,
        pdf_width=600.0,
        pdf_height=800.0,
    ) == [50.0, 100.0, 450.0, 700.0]


def test_logical_columns_preserve_source_colspan_structure():
    html = (
        "<table>"
        "<tr><td>A</td><td colspan=\"2\">B</td></tr>"
        "<tr><td>C</td><td>D</td><td>E</td></tr>"
        "</table>"
    )

    cells, column_count = _logical_cell_columns(html)

    assert cells == [(0, 1), (1, 3), (0, 1), (1, 2), (2, 3)]
    assert column_count == 3


def test_column_runs_prevent_adjacent_cells_from_becoming_one_text_run():
    html = "<table><tr><td>A1A2</td><td>N1N2</td></tr></table>"
    whole_table_runs = [
        [_run("A1 N1", 0, 80, 0)],
        [_run("A2 N2", 0, 80, 1)],
    ]
    runs_by_column = [
        [
            [_run("A1", 0, 20, 0)],
            [_run("A2", 0, 20, 1)],
        ],
        [
            [_run("N1", 50, 70, 0)],
            [_run("N2", 50, 70, 1)],
        ],
    ]

    corrected, changed_cells, added_breaks = _restore_table_cells(
        html,
        whole_table_runs,
        logical_cells=[(0, 1), (1, 2)],
        runs_by_column=runs_by_column,
    )

    assert corrected == (
        "<table><tr><td>A1<br>A2</td><td>N1<br>N2</td></tr></table>"
    )
    assert changed_cells == 2
    assert added_breaks == 2


def test_complete_single_line_prevents_borrowing_digits_from_other_rows():
    html = "<table><tr><td>35</td></tr></table>"
    runs_by_line = [
        [_run("35", 0, 20, 0)],
        [_run("3", 0, 10, 1)],
        [_run("5", 0, 10, 2)],
    ]

    corrected, changed_cells, added_breaks = _restore_table_cells(
        html,
        runs_by_line,
    )

    assert corrected == html
    assert changed_cells == 0
    assert added_breaks == 0


def test_related_continuation_page_single_line_prevents_wrong_split():
    html = "<table><tr><td>57</td></tr></table>"
    current_page_runs = [
        [_run("5", 0, 10, 0)],
        [_run("7", 0, 10, 1)],
    ]

    corrected, changed_cells, added_breaks = _restore_table_cells(
        html,
        current_page_runs,
        known_single_line_texts={"57"},
    )

    assert corrected == html
    assert changed_cells == 0
    assert added_breaks == 0


def test_multiple_possible_multiline_matches_are_left_unchanged():
    html = "<table><tr><td>A1A2</td></tr></table>"
    runs_by_line = [
        [_run("A1", 0, 20, 0)],
        [_run("A2", 0, 20, 1)],
        [_run("A1", 0, 20, 2)],
        [_run("A2", 0, 20, 3)],
    ]

    corrected, changed_cells, added_breaks = _restore_table_cells(
        html,
        runs_by_line,
    )

    assert corrected == html
    assert changed_cells == 0
    assert added_breaks == 0


def test_cell_scoped_runs_take_priority_over_unrelated_column_rows():
    html = "<table><tr><td>57</td></tr></table>"
    whole_column_runs = [
        [_run("5", 0, 10, 0)],
        [_run("7", 0, 10, 1)],
    ]
    runs_by_cell = [[[_run("57", 0, 20, 0)]]]

    corrected, changed_cells, added_breaks = _restore_table_cells(
        html,
        whole_column_runs,
        logical_cells=[LogicalCell(0, 1, 0, 1)],
        runs_by_cell=runs_by_cell,
    )

    assert corrected == html
    assert changed_cells == 0
    assert added_breaks == 0


def test_logical_layout_tracks_rowspan_and_colspan_ranges():
    html = (
        "<table>"
        '<tr><td rowspan="2">A</td><td colspan="2">B</td></tr>'
        "<tr><td>C</td><td>D</td></tr>"
        "</table>"
    )

    cells, row_count, column_count = _logical_cell_layout(html)

    assert cells == [
        LogicalCell(0, 2, 0, 1),
        LogicalCell(0, 1, 1, 3),
        LogicalCell(1, 2, 1, 2),
        LogicalCell(1, 2, 2, 3),
    ]
    assert row_count == 2
    assert column_count == 3


class _FakeVectorObject:
    type = 2

    def __init__(self, bounds):
        self._bounds = bounds

    def get_bounds(self):
        return self._bounds


class _FakePage:
    def __init__(self, objects):
        self._objects = objects

    def get_objects(self):
        return iter(self._objects)


def test_segmented_vector_lines_are_combined_into_grid_boundaries():
    objects = []

    # PDF 对象使用左下角原点。每条完整边界故意拆成两段，验证代码按
    # 联合覆盖长度识别边界，而不是要求单个对象贯穿整张表。
    for x in (0.0, 50.0, 100.0):
        objects.extend([
            _FakeVectorObject((x, 50.0, x + 0.5, 100.0)),
            _FakeVectorObject((x, 0.0, x + 0.5, 50.0)),
        ])
    for top_down_y in (0.0, 50.0, 100.0):
        pdf_y = 100.0 - top_down_y
        objects.extend([
            _FakeVectorObject((0.0, pdf_y, 50.0, pdf_y + 0.5)),
            _FakeVectorObject((50.0, pdf_y, 100.0, pdf_y + 0.5)),
        ])

    page = _FakePage(objects)
    bbox = [0.0, 0.0, 100.0, 100.0]

    column_boundaries = _detect_table_column_boundaries(
        page,
        bbox,
        pdf_height=100.0,
        expected_columns=2,
    )
    row_boundaries = _detect_table_row_boundaries(
        page,
        bbox,
        pdf_height=100.0,
        expected_rows=2,
    )

    assert column_boundaries is not None
    assert row_boundaries is not None
    assert len(column_boundaries) == 3
    assert len(row_boundaries) == 3
