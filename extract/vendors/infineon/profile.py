"""注册 Infineon 的字段与输出配置规则，共享主流程保持不变。"""

from extract.vendors.base import VendorProfile
from extract.vendors.infineon.package_handlers import resolve_infineon_scopes
from extract.vendors.infineon.table_handlers import (
    classify_infineon_header, match_infineon_table, prepare_infineon_tables,
)


PROFILE = VendorProfile(
    name="INFINEON", display_name="Infineon",
    description="识别物理引脚定义表，并区分明确的单输出和双输出配置。",
    header_classifier=classify_infineon_header,
    table_matcher=match_infineon_table,
    source_table_repairer=prepare_infineon_tables,
    package_scope_resolver=resolve_infineon_scopes,
)
