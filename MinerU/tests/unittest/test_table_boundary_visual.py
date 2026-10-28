from io import BytesIO

import pypdfium2
from reportlab.pdfgen import canvas

from mineru.utils.table_boundary_visual import TableBoundaryVisualVerifier
from mineru.utils.table_merge import build_table_state_from_html, perform_table_merge


HEADER = "<tr><td>Pin Number</td><td>Pin name</td><td>Type</td></tr>"


def _html(rows):
    return "<table>" + HEADER + "".join(
        "<tr>" + "".join("<td>" + value + "</td>" for value in row) + "</tr>"
        for row in rows
    ) + "</table>"


def _pdf(pages, grid=True):
    stream = BytesIO()
    painter = canvas.Canvas(stream, pagesize=(300, 300))
    for rows in pages:
        if grid:
            for x in (20, 90, 200, 280):
                painter.line(x, 100, x, 280)
            for y in (280, 240, 170, 100):
                painter.line(20, y, 280, y)
        for y, row in zip((255, 195, 125), [("Pin Number", "Pin name", "Type"), *rows]):
            for x, text in zip((25, 95, 205), row):
                painter.drawString(x, y, text)
        painter.showPage()
    painter.save()
    return pypdfium2.PdfDocument(stream.getvalue())


def _states(before, after):
    states = [build_table_state_from_html(_html(rows)) for rows in (before, after)]
    for state in states:
        state.body_span = {"bbox": [20, 20, 280, 200]}
    states[1].owner_block["cell_merge"] = [1, 1, 1]
    return states


def _verify(before, after, grid=True):
    doc = _pdf([before, after], grid)
    states = _states(before, after)
    pages = [{"page_idx": i, "page_size": [300, 300]} for i in (0, 1)]
    try:
        return TableBoundaryVisualVerifier(doc).independent_rows(*pages, *states, 1, [1, 1, 1])
    finally:
        doc.close()


def test_visual_grid_confirms_independent_rows_with_identical_names():
    before = [("4", "GPIO", "IO"), ("7", "VDD", "S")]
    after = [("11", "VDD", "S"), ("13", "RESET", "I")]
    assert _verify(before, after)
    previous, current = _states(before, after)
    perform_table_merge(previous, current, {"blocks": []}, [], visual_independent=True)
    result = [[cell.get_text() for cell in row.find_all("td")] for row in previous.rows]
    assert result[-3:] == [["7", "VDD", "S"], ["11", "VDD", "S"], ["13", "RESET", "I"]]


def test_continuation_without_independent_identifier_is_not_guessed():
    before = [("4", "GPIO", "IO"), ("7", "RESET", "I")]
    after = [("", "LINE", "I"), ("13", "NEXT", "I")]
    assert not _verify(before, after)


def test_missing_grid_cannot_be_visual_evidence():
    before = [("4", "GPIO", "IO"), ("7", "VDD", "S")]
    after = [("11", "VDD", "S"), ("13", "RESET", "I")]
    assert not _verify(before, after, grid=False)


def test_repeated_identifier_can_be_a_continuation():
    before = [("4", "GPIO", "IO"), ("7", "RESET", "I")]
    after = [("7", "RESET", "I"), ("13", "NEXT", "I")]
    assert not _verify(before, after)


def test_pdf_text_must_match_the_candidate_row():
    doc = _pdf([[("4", "GPIO", "IO"), ("7", "VDD", "S")],
                [("11", "VDD", "S"), ("13", "RESET", "I")]])
    previous, current = _states([("4", "GPIO", "IO"), ("7", "OTHER", "S")],
                                [("11", "VDD", "S"), ("13", "RESET", "I")])
    pages = [{"page_idx": i, "page_size": [300, 300]} for i in (0, 1)]
    try:
        assert not TableBoundaryVisualVerifier(doc).independent_rows(*pages, previous, current, 1, [1, 1, 1])
    finally:
        doc.close()
