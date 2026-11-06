"""注册 NEXPERIA 独立厂家流程。"""

from extract.vendors.base import VendorProfile
from extract.vendors.nexperia.package_handlers import resolve_nexperia_scopes
from extract.vendors.nexperia.table_handlers import (
    finalize_nexperia_record, match_nexperia_table, prepare_nexperia_tables, repair_nexperia_rows,
)


PROFILE = VendorProfile(
    name="NEXPERIA", display_name="Nexperia",
    description="适配 Pinning 引脚定义、表内范围与安装底座端子。",
    source_table_repairer=prepare_nexperia_tables,
    table_matcher=match_nexperia_table,
    row_repairer=repair_nexperia_rows,
    package_scope_resolver=resolve_nexperia_scopes,
    record_finalizer=finalize_nexperia_record,
)
