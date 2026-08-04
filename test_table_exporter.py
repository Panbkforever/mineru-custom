import json

from table_exporter import export_tables_from_parse_artifacts


def _table(header, rows):
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in ([header] + rows)
    )
    return f"<table>{body}</table>"


def _write_artifacts(tmp_path, source_pages, final_markdown):
    middle_path = tmp_path / "sample_middle.json"
    final_path = tmp_path / "sample.json"
    middle_path.write_text(
        json.dumps(
            {
                "pdf_info": [
                    {
                        "page_idx": page_idx,
                        "para_blocks": [
                            {"type": "table", "html": table_html}
                            for table_html in tables
                        ],
                    }
                    for page_idx, tables in source_pages
                ]
            }
        ),
        encoding="utf-8",
    )
    final_path.write_text(
        json.dumps({"markdown": final_markdown}),
        encoding="utf-8",
    )
    return final_path, middle_path


def test_exports_all_final_tables_with_one_based_pages(tmp_path):
    first = _table(["PIN", "NAME"], [["A1", "VDD"]])
    second = _table(["PIN", "NAME"], [["B2", "GND"]])
    final_path, middle_path = _write_artifacts(
        tmp_path,
        [(2, [first]), (7, [second])],
        f"Table 1. Power Pins\n\n{first}\n\nTable 2. Ground Pins\n\n{second}",
    )

    result = export_tables_from_parse_artifacts(
        final_path,
        middle_path,
        "sample.pdf",
    )

    assert result["pdf_name"] == "sample.pdf"
    assert result["table_count"] == 2
    assert [item["page_no"] for item in result["table_list"]] == [3, 8]
    assert [item["title"] for item in result["table_list"]] == [
        "Table 1. Power Pins",
        "Table 2. Ground Pins",
    ]


def test_split_postprocessed_tables_inherit_the_original_page(tmp_path):
    header = ["PIN", "NAME"]
    original = _table(header, [["A1", "VDD"], ["A2", "GND"]])
    first_part = _table(header, [["A1", "VDD"]])
    second_part = _table(header, [["A2", "GND"]])
    following = _table(header, [["B1", "CLK"]])
    final_path, middle_path = _write_artifacts(
        tmp_path,
        [(4, [original]), (5, [following])],
        f"Table 1. Pins\n{first_part}\n{second_part}\nTable 2. Clock\n{following}",
    )

    result = export_tables_from_parse_artifacts(
        final_path,
        middle_path,
        "sample.pdf",
    )

    assert result["table_count"] == 3
    assert [item["page_no"] for item in result["table_list"]] == [5, 5, 6]
