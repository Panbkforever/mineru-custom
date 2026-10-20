"""注册 GigaDevice 的表格字段、封装范围和占位行处理。"""

from extract.vendors.base import VendorProfile
from extract.vendors.giga.package_handlers import resolve_giga_scopes
from extract.vendors.giga.table_handlers import match_giga_table, repair_giga_table, should_keep_giga_record


PROFILE = VendorProfile(
    name="GIGA",
    display_name="GigaDevice",
    description="依据明确封装表题或列头绑定独立引脚映射。",
    table_matcher=match_giga_table,
    table_repairer=repair_giga_table,
    record_filterer=should_keep_giga_record,
    package_scope_resolver=resolve_giga_scopes,
)
