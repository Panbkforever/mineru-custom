"""TI 卡槽维度不能误当作封装。"""
from extract.multi_package_extractor import MultiPackagePlan, PackageBinding
from extract.package_catalog_resolver import PackageTargetTable
from extract.vendors.ti.package_handlers import resolve_ti_slot_package_scopes
from extract.vendors.ti.table_handlers import ti_binding_group


def test_two_slots_share_drawing_packages():
    headers = ('TERMINAL SLOT A† PDV', 'TERMINAL SLOT A† GHK',
               'TERMINAL SLOT B‡ PDV', 'TERMINAL SLOT B‡ GHK')
    table = PackageTargetTable(1, 1, 'Pin Functions', '', (), headers)
    labels = ('A PDV', 'A GHK', 'B PDV', 'B GHK')
    plan = MultiPackagePlan(True, 'package_columns', tuple(
        PackageBinding(label, i, None, None) for i, label in enumerate(labels)))
    result = resolve_ti_slot_package_scopes([table], {1: plan})
    assert result is not None and len(result.entries) == 2
    assert [result.assignment_for(1, i).pkg for i in range(4)] == ['PDV', 'GHK', 'PDV', 'GHK']
    assert ti_binding_group('Pin Functions', 'A PDV', headers[0]) == 'Pin Functions (SLOT A)'
    assert ti_binding_group('Pin Functions', 'B PDV', headers[2]) == 'Pin Functions (SLOT B)'


def test_missing_slot_evidence_does_not_take_over():
    table = PackageTargetTable(1, 1, 'Pin Functions', '', (), ('PDV', 'GHK'))
    plan = MultiPackagePlan(True, 'package_columns', (
        PackageBinding('PDV', 0, None, None), PackageBinding('GHK', 1, None, None)))
    assert resolve_ti_slot_package_scopes([table], {1: plan}) is None
