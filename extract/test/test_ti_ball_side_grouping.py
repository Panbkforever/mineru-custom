"""TI 同一封装的上下层编号应保留来源。"""
from unittest.mock import patch
import extract.pin_package_extractor as extractor
from extract.vendors.ti.profile import PROFILE
from extract.vendors.base import VendorProfile
from extract.multi_package_extractor import MultiPackagePlan, PackageBinding
from extract.pin_package_extractor import ball_side_suffix_for_local_slot
from extract.pin_package_extractor import TableCandidate, TableDecision, ColumnDecision
from extract.package_catalog_resolver import PackageAssignment, PackageCatalogEntry, PackageCatalogResolution


def test_only_ti_changes_ball_side_package_policy():
    assert not PROFILE.split_ball_side_packages
    assert VendorProfile(name='OTHER', display_name='Other').split_ball_side_packages
    plan = MultiPackagePlan(True, 'package_columns', (
        PackageBinding('BOTTOM', 0, 2, None),
        PackageBinding('TOP', 1, 2, None),
    ))
    headers = ['BALL BOTTOM [1]', 'BALL TOP [2]', 'PIN NAME']
    assert ball_side_suffix_for_local_slot(plan, 0, headers) == 'bottom'
    assert ball_side_suffix_for_local_slot(plan, 1, headers) == 'top'


def test_ti_ball_sides_share_one_package_and_keep_both_groups():
    """两列属于同一封装时保留各自来源及数据。"""
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
    assert len(result) == 1 and result[0]['pkg'] == 'FCCSP'
    groups = {g['group']: [x['pin_no'] for x in g['pin_list']] for g in result[0]['group_list']}
    assert groups == {'Ball Characteristics (BOTTOM)': ['A1'], 'Ball Characteristics (TOP)': ['B1']}
