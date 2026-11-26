"""TI 配置矩阵的表级判断测试。"""
from extract.vendors.ti.table_handlers import match_ti_table


def test_ioset_matrix_is_not_a_package_table():
    headers = ['Signal', 'IOSET1 BALL', 'IOSET1 MUX', 'IOSET2 BALL', 'IOSET2 MUX']
    result = match_ti_table('McSPI3/4 IOSETs', headers, [])
    assert result is not None and not result.should_extract
    assert match_ti_table('Pin Functions', headers, []) is None


def test_timer_input_output_matrix_is_not_a_package_table():
    headers = ['TIMER_A3 SIGNAL CONNECTIONS INPUT PIN NUMBER',
               'TIMER_A3 SIGNAL CONNECTIONS DEVICE INPUT SIGNAL',
               'TIMER_A3 SIGNAL CONNECTIONS OUTPUT PIN NUMBER']
    result = match_ti_table('', headers, [])
    assert result is not None and not result.should_extract
    unprefixed = ['INPUT PIN NUMBER RGC', 'DEVICE INPUT SIGNAL', 'OUTPUT PIN NUMBER RGC']
    assert not match_ti_table('TB0 Signal Connections', unprefixed, []).should_extract
    assert match_ti_table('Pin Functions', ['PIN NO.', 'PIN NAME', 'INPUT', 'OUTPUT'], []) is None


def test_input_multiplexer_matrix_is_not_a_pin_table():
    """仅在标题与控制矩阵表头同时出现时排除。"""
    headers = ['SIGNAL NAME', 'DEDICATED INPUTS 144 PGE', 'MULTIPLEXED INPUTS 144 PGE',
               'INPUT MULTIPLEXOR CONTROL BIT1']
    assert not match_ti_table('Input Multiplexing and Control for All Packages', headers, []).should_extract
    assert match_ti_table('Pin Functions', headers, []) is None
