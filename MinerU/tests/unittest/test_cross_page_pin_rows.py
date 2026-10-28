from mineru.utils.table_merge import build_table_state_from_html, perform_table_merge


def _state(rows, flags=None):
    state = build_table_state_from_html("<table>" + rows + "</table>")
    state.owner_block["cell_merge"] = flags or []
    return state


def _texts(state):
    return [[cell.get_text() for cell in row.find_all(["td", "th"])] for row in state.rows]


def _merge(before, after, flags):
    previous = _state(before)
    current = _state(after, flags)
    perform_table_merge(previous, current, {"blocks": []}, [])
    return _texts(previous)


HEADER = "<tr><td>Pin Number</td><td>Pin name</td><td>Type</td></tr>"


def test_independent_rows_with_multiple_evidence_are_preserved():
    result = _merge(
        HEADER + "<tr><td>10</td><td>PA0</td><td>I/O</td></tr>",
        HEADER + "<tr><td>11</td><td>PA1</td><td>I/O</td></tr>"
        + "<tr><td>12</td><td>PA2</td><td>I/O</td></tr>",
        [1, 1, 1],
    )
    assert result[-3:] == [["10", "PA0", "I/O"], ["11", "PA1", "I/O"], ["12", "PA2", "I/O"]]


def test_genuine_continuation_keeps_existing_merge():
    result = _merge(
        HEADER + "<tr><td>1</td><td>RESET_</td><td>input</td></tr>",
        HEADER + "<tr><td></td><td>LINE</td><td> signal</td></tr>"
        + "<tr><td>2</td><td>VDD</td><td>power</td></tr>",
        [1, 1, 1],
    )
    assert result[-2:] == [["1", "RESET_LINE", "input signal"], ["2", "VDD", "power"]]


def test_partial_flags_keep_existing_behavior():
    result = _merge(
        HEADER + "<tr><td>1</td><td>RESET_</td><td>input</td></tr>",
        HEADER + "<tr><td></td><td>LINE</td><td> signal</td></tr>"
        + "<tr><td>2</td><td>VDD</td><td>power</td></tr>",
        [0, 1, 1],
    )
    assert result[-2:] == [["1", "RESET_LINE", "input signal"], ["2", "VDD", "power"]]


def test_non_pin_table_and_ambiguous_sequence_keep_existing_behavior():
    other = HEADER.replace("Pin name", "Signal")
    rows = "<tr><td>11</td><td>PA1</td><td>I/O</td></tr><tr><td>12</td><td>PA2</td><td>I/O</td></tr>"
    result = _merge(other + "<tr><td>10</td><td>PA0</td><td>I/O</td></tr>", other + rows, [1, 1, 1])
    assert result[-2] == ["1011", "PA0PA1", "I/OI/O"]
    result = _merge(HEADER + "<tr><td>9</td><td>PA0</td><td>I/O</td></tr>", HEADER + rows, [1, 1, 1])
    assert result[-2] == ["911", "PA0PA1", "I/OI/O"]


def test_rowspan_is_not_guessed():
    result = _merge(
        HEADER + "<tr><td>10</td><td>PA0</td><td>I/O</td></tr>",
        HEADER + "<tr><td>11</td><td>PA1</td><td>I/O</td></tr>"
        + "<tr><td>12</td><td rowspan='2'>PA2</td><td>I/O</td></tr>"
        + "<tr><td>13</td><td>I/O</td></tr>",
        [1, 1, 1],
    )
    assert result[-3] == ["1011", "PA0PA1", "I/OI/O"]
