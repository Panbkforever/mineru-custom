"""TI 上下层编号保持项目原有的外层封装拆分行为。"""
from unittest.mock import patch
import extract.pin_package_extractor as extractor
from extract.vendors.ti.profile import PROFILE
from extract.multi_package_extractor import MultiPackagePlan, PackageBinding
from extract.pin_package_extractor import ball_side_suffix_for_local_slot
from extract.pin_package_extractor import TableCandidate, TableDecision, ColumnDecision
from extract.package_catalog_resolver import PackageAssignment, PackageCatalogEntry, PackageCatalogResolution


def test_ti_detects_ball_sides():
    plan = MultiPackagePlan(True, 'package_columns', (
        PackageBinding('BOTTOM', 0, 2, None),
        PackageBinding('TOP', 1, 2, None),
    ))
    headers = ['BALL BOTTOM [1]', 'BALL TOP [2]', 'PIN NAME']
    assert ball_side_suffix_for_local_slot(plan, 0, headers) == 'bottom'
    assert ball_side_suffix_for_local_slot(plan, 1, headers) == 'top'


def test_ti_ball_sides_keep_separate_packages():
    """上下层继续输出到两个独立结果。"""
    table = TableCandidate(
        html='<table><tr><td>BALL BOTTOM</td><td>BALL TOP</td><td>PIN NAME</td></tr>'
             '<tr><td>A1</td><td>B1</td><td>VDD</td></tr></table>',
        page_idx=0, title='Ball Characteristics', group_context='Ball Characteristics',
    )
    columns = [ColumnDecision(0, 'BALL BOTTOM', 'pin_no'),
               ColumnDecision(1, 'BALL TOP', 'pin_no'),
               ColumnDecision(2, 'PIN NAME', 'pin_name')]
    entry = PackageCatalogEntry(package_key='slot:0', package_type='FCCSP')
    assignment = PackageAssignment('slot:0', 'FCCSP', 'test')
    resolution = PackageCatalogResolution([entry], {(0, 0): assignment, (0, 1): assignment})
    with patch.object(extractor, 'get_current_vendor_profile', return_value=PROFILE), \
         patch.object(extractor, 'decide_all_tables', return_value={0: TableDecision(True, columns=columns)}), \
         patch.object(extractor, 'resolve_document_package_catalog', return_value=resolution):
        result = extractor.extract_pin_package_info_from_table_candidates([table])
    packages = {p['pkg']: p for p in result if p['group_list']}
    assert set(packages) == {'FCCSP_bottom', 'FCCSP_top'}
    assert packages['FCCSP_bottom']['group_list'][0]['pin_list'][0]['pin_no'] == 'A1'
    assert packages['FCCSP_top']['group_list'][0]['pin_list'][0]['pin_no'] == 'B1'
