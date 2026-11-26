"""TI 连续球号修复测试。"""
from extract.vendors.ti.table_handlers import repair_ti_rows


def test_split_unambiguous_ball_numbers_without_filtering_rows():
    headers = ['SIGNAL', 'ZDY/ GDY BALL#', 'ZZG BALL#', 'DESCRIPTION']
    rows = [['CLK', 'U15L15', 'P14M15', 'clock'],
            ['DIN', 'L14P12U14', 'P19V15W16', 'data'],
            ['NONE', '', '-', 'unchanged']]
    result = repair_ti_rows('', headers, rows)
    assert result == [['CLK', 'U15, L15', 'P14, M15', 'clock'],
                      ['DIN', 'L14, P12, U14', 'P19, V15, W16', 'data'],
                      ['NONE', '', '-', 'unchanged']]
    assert rows[0][1] == 'U15L15'


def test_do_not_guess_numeric_or_non_number_fields():
    headers = ['SIGNAL NAME', 'PIN NO. PGF', 'BALL NO. GHH']
    rows = [['A1B2', '132133', 'A14B13'], ['C2', '150137', 'A1']]
    assert repair_ti_rows('', headers, rows) == [['A1B2', '132133', 'A14, B13'], ['C2', '150137', 'A1']]
