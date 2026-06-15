import unittest
from unittest.mock import patch

from post_table.expand_rowspan import expand_colspan, expand_rowspan
from post_table.min_max_coordinate_correct import (
    _correct_single_pdf_value_duplicated_in_html,
    _find_explicit_colspan_ranges,
    _parse_expanded_rows,
    _preserve_explicit_value_span,
    _value_header_groups,
)


class MinMaxCoordinateCorrectTest(unittest.TestCase):
    def test_value_header_groups_include_typ_and_nom(self):
        self.assertEqual(_value_header_groups(["MIN", "TYP", "MAX"]), [(0, 3)])
        self.assertEqual(_value_header_groups(["MIN", "NOM", "MAX"]), [(0, 3)])
        self.assertEqual(
            _value_header_groups(["MIN", "MAX", "MIN", "MAX"]),
            [(0, 2), (2, 4)],
        )

    def test_duplicate_without_colspan_keeps_nearest_column(self):
        cells = [
            {"open": "<td>", "inner": "0.65 x VDD", "close": "</td>"},
            {"open": "<td>", "inner": "0.65 x VDD", "close": "</td>"},
            {"open": "<td>", "inner": "", "close": "</td>"},
        ]
        with patch(
            "post_table.min_max_coordinate_correct._find_pdf_value_centers",
            return_value=[100.0],
        ):
            changed = _correct_single_pdf_value_duplicated_in_html(
                cells=cells,
                value_column_indexes=[0, 1, 2],
                html_values=[cell["inner"] for cell in cells],
                centers=[100.0, 200.0, 300.0],
                value_groups=[(0, 3)],
                explicit_colspans=[],
                text_page=object(),
                row_y0=0.0,
                row_y1=10.0,
                page_height=100.0,
            )

        self.assertTrue(changed)
        self.assertEqual([cell["inner"] for cell in cells], ["0.65 x VDD", "", ""])

    def test_explicit_shared_colspan_is_fully_expanded_and_preserved(self):
        table = (
            "<table>"
            "<tr><td>PARAMETER</td><td>DESCRIPTION</td>"
            "<td>MIN</td><td>NOM</td><td>MAX</td><td>UNIT</td></tr>"
            "<tr><td>VPP</td><td>Normal operation</td>"
            '<td colspan="3">0</td><td>V</td></tr>'
            "</table>"
        )
        rowspan_expanded = expand_rowspan(table)
        explicit_colspans = _find_explicit_colspan_ranges(rowspan_expanded)
        rows = _parse_expanded_rows(expand_colspan(rowspan_expanded))
        value_cells = rows[1][2:5]

        self.assertEqual(explicit_colspans[1], [(2, 5)])
        self.assertEqual([cell["inner"] for cell in value_cells], ["0", "0", "0"])

        with patch(
            "post_table.min_max_coordinate_correct._find_pdf_value_centers",
            return_value=[200.0],
        ):
            changed = _correct_single_pdf_value_duplicated_in_html(
                cells=rows[1],
                value_column_indexes=[2, 3, 4],
                html_values=[cell["inner"] for cell in value_cells],
                centers=[100.0, 200.0, 300.0],
                value_groups=[(0, 3)],
                explicit_colspans=explicit_colspans[1],
                text_page=object(),
                row_y0=0.0,
                row_y1=10.0,
                page_height=100.0,
            )

        self.assertFalse(changed)
        self.assertEqual(
            [rows[1][index]["inner"] for index in (2, 3, 4)],
            ["0", "0", "0"],
        )

    def test_false_numeric_colspan_is_not_preserved(self):
        self.assertFalse(
            _preserve_explicit_value_span(
                value="49",
                duplicate_columns=[3, 4, 5],
                group_columns=[3, 4, 5],
                explicit_colspans=[(3, 6)],
                value_center=200.0,
                group_centers=[100.0, 200.0, 300.0],
            )
        )
        self.assertTrue(
            _preserve_explicit_value_span(
                value="NC(2)",
                duplicate_columns=[3, 4, 5],
                group_columns=[3, 4, 5],
                explicit_colspans=[(3, 6)],
                value_center=200.0,
                group_centers=[100.0, 200.0, 300.0],
            )
        )


if __name__ == "__main__":
    unittest.main()
