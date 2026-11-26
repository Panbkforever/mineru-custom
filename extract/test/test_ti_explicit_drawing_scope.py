"""TI 多页 drawing 表头绑定测试。"""
from extract.package_catalog_resolver import PackageTargetTable
from extract.multi_package_extractor import MultiPackagePlan, PackageBinding
from extract.vendors.ti.package_handlers import resolve_ti_explicit_drawing_scopes
from extract.vendors.ti.package_handlers import resolve_ti_title_drawing_scopes


def _target(i):
    return PackageTargetTable(i, i, "Signal Descriptions", "", (), ())


def _plan(labels):
    return MultiPackagePlan(True, "package_columns", bindings=tuple(
        PackageBinding(package=label, pin_no_column=i, pin_name_column=3, type_column=None)
        for i, label in enumerate(labels)
    ))


def test_ti_drawing_columns_follow_labels_across_pages():
    """续表中的空格变化和列序变化不改变封装归属。"""
    tables = [_target(1), _target(2)]
    plans = {1: _plan(['PGF PTPPIN', 'GHHBALL', 'GJZBALL']),
             2: _plan(['GJZ BALL', 'PGF P TP PIN', 'GHH BALL'])}
    result = resolve_ti_explicit_drawing_scopes(tables, plans)
    assert result is not None
    assert [result.assignment_for(1, i).pkg for i in range(3)] == ['PGFPTP', 'GHH', 'GJZ']
    assert [result.assignment_for(2, i).pkg for i in range(3)] == ['GJZ', 'PGFPTP', 'GHH']


def test_ti_scope_leaves_non_drawing_or_unmatched_tables_to_main_flow():
    """结构不完整时不接管，不能把 IOSET 或 INPUT 当作封装。"""
    assert resolve_ti_explicit_drawing_scopes([_target(1)], {1: _plan(['IOSET1 BALL', 'IOSET2 BALL'])}) is None
    assert resolve_ti_explicit_drawing_scopes([_target(1)], {1: _plan(['INPUT', 'OUTPUT'])}) is None
    assert resolve_ti_explicit_drawing_scopes([_target(1), _target(2)], {
        1: _plan(['GHHBALL', 'GJZBALL']), 2: _plan(['GHHBALL', 'XYZBALL'])
    }) is None


def test_ti_explicit_title_and_header_bind_drawing():
    """多个物理封装章节只有标题和编号列一致才接管。"""
    tables = [PackageTargetTable(1, 1, 'Table 3-1. PGE Pins', '', (), ('SIGNAL NAME', 'TERMINAL 144 PGE')),
              PackageTargetTable(2, 2, 'Table 3-2. PZ Pins', '', (), ('SIGNAL NAME', 'TERMINAL 100 PZ'))]
    plans = {1: MultiPackagePlan(False, 'single_package'), 2: MultiPackagePlan(False, 'single_package')}
    result = resolve_ti_title_drawing_scopes(tables, plans)
    assert result is not None
    assert result.assignment_for(1, 0).pkg == 'PGE'
    assert result.assignment_for(2, 0).pkg == 'PZ'
    tables[1] = PackageTargetTable(2, 2, 'Table 3-2. PZ Pins', '', (), ('SIGNAL NAME', 'TERMINAL 100 ABC'))
    assert resolve_ti_title_drawing_scopes(tables, plans) is None


def test_terminal_number_headers_confirm_short_drawing_labels():
    """带 TERMINAL NO 的短封装标签按表头固定，续表顺序可不同。"""
    tables = [PackageTargetTable(1, 1, 'Terminal Functions', '', (), ('TERMINAL NO. RGC', 'TERMINAL NO. ZQE')),
              PackageTargetTable(2, 2, 'Terminal Functions', '', (), ('TERMINAL NO. ZQE', 'TERMINAL NO. RGC'))]
    plans = {1: _plan(['RGC', 'ZQE']), 2: _plan(['ZQE', 'RGC'])}
    result = resolve_ti_explicit_drawing_scopes(tables, plans)
    assert result is not None
    assert [result.assignment_for(1, i).pkg for i in range(2)] == ['RGC', 'ZQE']
    assert [result.assignment_for(2, i).pkg for i in range(2)] == ['ZQE', 'RGC']
