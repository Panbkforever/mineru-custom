from post_table.restore_cell_line_breaks import (
    TextRun,
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
