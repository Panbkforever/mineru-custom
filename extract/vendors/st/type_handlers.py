"""ST 最多两个 type 字段，按源表顺序合并到一个公开字段。"""

ST_TYPE_SELECTION_RULE = (
    "For ST tables, identify columns describing pin/signal type or I/O structure as type. "
    "If one column qualifies select it; if two or more qualify select only the first "
    "two from left to right by column_index. Deterministic code joins the selected "
    "values with an underscore into a single type field. Do not select a third type column."
)


def select_st_type_columns(columns, rows=None):
    """只读取已判定为 type 的映射，按列号保留前两个不同列。"""
    types = {}
    for column in columns:
        if column.field_name in {'type', 'io_type'}:
            types.setdefault(column.index, column)
    selected = set(sorted(types)[:2])
    result = []
    seen = set()
    for column in columns:
        if column.field_name not in {'type', 'io_type'}:
            result.append(column)
        elif column.index in selected and column.index not in seen:
            result.append(column)
            seen.add(column.index)
    return result


def resolve_st_type_value(row, columns):
    """非空值用下划线连接；不去重，不推断其他列，不改变数据行。"""
    selected = sorted((c for c in select_st_type_columns(columns)
                       if c.field_name in {'type', 'io_type'}), key=lambda c: c.index)
    if not selected:
        return None
    values = [str(row[c.index]).strip() if c.index < len(row) else '' for c in selected]
    return '_'.join(value for value in values if value)
