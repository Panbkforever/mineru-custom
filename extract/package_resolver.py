"""确定性识别文档中的封装，并在逐行提取前完成表格封装归属。

本模块只处理输出 JSON 最外层的 ``pkg``，不参与以下工作：

* 不判断表格是否需要提取；
* 不判断或修改 ``pin_no``、``pin_name``、``type`` 字段；
* 不生成引脚记录；
* 不修改表格的 ``group``；
* 不调用大模型。

处理流程固定为四个阶段：

1. 扫描全文中的所有表格标题、当前章节标题和封装信息表，建立文档级
   ``PackageRegistry``；被后续引脚提取过滤掉的订购信息表仍可提供封装证据。
2. 接收已经完成的多封装计划。多个封装专属编号列、package 控制列和
   package 分段行属于最强证据，直接登记为多封装归属。
3. 对单封装目标表依次检查当前表题、当前章节、表内 package 列和同章节
   唯一归属。上一章节标题只用于输出 group，不能继承为当前表的封装。
4. 仍未归属的表使用已明确封装表的引脚编号/名称集合做保守关联；证据冲突
   或分数不足时保持空字符串，不能为了减少空 pkg 而猜测。

封装实体按明确的 package drawing/code、封装名称、封装家族和 pin count
组织。相同字段名不构成封装相同证据；不同 drawing code 永远不会仅因字段
名称或封装家族相同而合并。
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence


class ColumnLike(Protocol):
    """字段判断对象需要提供的最小接口。"""

    index: int
    raw_header: str
    field_name: str


class PackageBindingLike(Protocol):
    """多封装绑定对象需要提供的最小接口。"""

    package: str
    pin_no_column: int
    pin_name_column: int | None
    row_indexes: frozenset[int] | None


class MultiPackagePlanLike(Protocol):
    """多封装计划对象需要提供的最小接口。"""

    is_multi_package: bool
    mode: str
    bindings: Sequence[PackageBindingLike]


@dataclass(frozen=True)
class PackageTableSource:
    """全文扫描阶段需要的一张表及其章节上下文。"""

    table_id: int
    title: str
    group_context: str
    previous_chapter_titles: tuple[str, ...]
    current_chapter_titles: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class PackageTargetTable:
    """已经通过表格/字段判断、等待确定 pkg 的目标表。"""

    table_id: int
    title: str
    current_chapter_titles: tuple[str, ...]
    headers: tuple[str, ...]
    data_rows: tuple[tuple[str, ...], ...]
    columns: tuple[ColumnLike, ...]
    declared_package: str = ""
    included_row_indexes: frozenset[int] | None = None


@dataclass(frozen=True)
class PackageEvidence:
    """一条封装候选的来源，用于调试和冲突分析。"""

    source: str
    table_id: int
    detail: str
    confidence: float


@dataclass
class PackageCandidate:
    """文档中一个规范化后的封装实体及其别名。"""

    key: str
    aliases: list[str] = field(default_factory=list)
    drawing_code: str = ""
    family: str = ""
    pin_count: str = ""
    evidence: list[PackageEvidence] = field(default_factory=list)

    @property
    def display(self) -> str:
        """按发现顺序输出全部别名，满足同一封装追加名称的项目规则。"""

        return " | ".join(self.aliases)


@dataclass(frozen=True)
class TablePackageAssignment:
    """一张目标表在行提取前得到的封装归属。"""

    package_keys: tuple[str, ...] = ()
    mode: str = "unresolved"
    confidence: float = 0.0
    reason: str = ""


@dataclass
class PackageResolutionResult:
    """整篇文档的候选库和所有目标表归属。"""

    registry: "PackageRegistry"
    assignments: dict[int, TablePackageAssignment]

    def package_label(self, table_id: int) -> str:
        """返回单封装表的显示名称；多封装表由原绑定计划逐行读取。"""

        assignment = self.assignments.get(table_id)
        if assignment is None or len(assignment.package_keys) != 1:
            return ""
        return self.registry.display_for_key(assignment.package_keys[0])


@dataclass
class _PackageFingerprint:
    """已明确封装的引脚集合，只用于后续未知表的保守关联。"""

    pin_numbers: set[str] = field(default_factory=set)
    pin_names: set[str] = field(default_factory=set)


class PackageRegistry:
    """保存文档级封装实体，并提供不猜测的别名查询。"""

    def __init__(self) -> None:
        self.candidates: dict[str, PackageCandidate] = {}
        self._alias_to_keys: dict[str, set[str]] = {}

    def register(
        self,
        primary: str,
        *,
        aliases: Sequence[str] = (),
        drawing_code: str = "",
        family: str = "",
        pin_count: str = "",
        evidence: PackageEvidence,
    ) -> str:
        """登记一个封装实体；只有身份兼容且别名唯一时才合并。"""

        cleaned_labels = _unique_labels([primary, *aliases])
        if not cleaned_labels:
            return ""

        metadata = _derive_label_metadata(cleaned_labels, drawing_code, family, pin_count)
        primary_label = metadata["primary"]
        drawing_code = metadata["drawing_code"]
        family = metadata["family"]
        pin_count = metadata["pin_count"]
        cleaned_labels = _unique_labels([primary_label, *metadata["aliases"]])

        # 有 drawing code 时，它是强身份，只允许与同一 drawing 的候选合并。
        # 不能因为两个封装都属于 QFN/BGA 家族，就把先出现的 drawing 错当
        # 成整个家族。没有 drawing 时才允许使用全部别名做唯一兼容查询。
        identity_labels = [drawing_code] if drawing_code else cleaned_labels
        alias_keys: set[str] = set()
        for label in identity_labels:
            alias_keys.update(self._alias_to_keys.get(_normalize(label), set()))
        compatible = [
            key
            for key in alias_keys
            if _candidate_is_compatible(
                self.candidates[key],
                drawing_code=drawing_code,
                pin_count=pin_count,
            )
        ]

        if len(set(compatible)) == 1:
            key = compatible[0]
            candidate = self.candidates[key]
        elif len(set(compatible)) > 1 and not drawing_code:
            # 例如 QFN 同时指向 RGY 和 RGT，而当前文本只写了 QFN。
            # 这是歧义证据，不能创建第三个“通用 QFN”并误认为已经解析。
            return ""
        else:
            key = _candidate_key(primary_label, drawing_code, family, pin_count)
            key = self._make_unique_key(key, drawing_code, pin_count)
            candidate = self.candidates.setdefault(
                key,
                PackageCandidate(
                    key=key,
                    drawing_code=drawing_code,
                    family=family,
                    pin_count=pin_count,
                ),
            )

        if not candidate.drawing_code:
            candidate.drawing_code = drawing_code
        if not candidate.family:
            candidate.family = family
        if not candidate.pin_count:
            candidate.pin_count = pin_count
        for label in cleaned_labels:
            if label not in candidate.aliases:
                candidate.aliases.append(label)
            self._alias_to_keys.setdefault(_normalize(label), set()).add(candidate.key)
        if evidence not in candidate.evidence:
            candidate.evidence.append(evidence)
        return candidate.key

    def unique_key_for_label(self, value: str) -> str:
        """只有一个候选使用该别名时才返回 key，避免同名封装误归并。"""

        keys = self._alias_to_keys.get(_normalize(value), set())
        return next(iter(keys)) if len(keys) == 1 else ""

    def keys_for_label(self, value: str) -> set[str]:
        """返回标签对应的全部候选，供调用方显式识别歧义。"""

        return set(self._alias_to_keys.get(_normalize(value), set()))

    def keys_in_text(self, value: str) -> set[str]:
        """查找文本中明确出现且不歧义的已知封装别名。"""

        text = str(value or "")
        normalized_text = _normalize(text)
        result: set[str] = set()
        for alias, keys in self._alias_to_keys.items():
            if len(keys) != 1 or len(alias) < 2:
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", normalized_text):
                result.update(keys)
        return result

    def all_keys_in_text(self, value: str) -> set[str]:
        """返回文本中全部已知别名候选，包括一对多的歧义别名。"""

        normalized_text = _normalize(value)
        result: set[str] = set()
        for alias, keys in self._alias_to_keys.items():
            if len(alias) < 2:
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", normalized_text):
                result.update(keys)
        return result

    def keys_for_table_sources(self, table_id: int, sources: set[str]) -> set[str]:
        """返回某张表由指定局部证据直接发现的封装。"""

        result = set()
        for key, candidate in self.candidates.items():
            if any(
                evidence.table_id == table_id and evidence.source in sources
                for evidence in candidate.evidence
            ):
                result.add(key)
        return result

    def display_for_key(self, key: str) -> str:
        candidate = self.candidates.get(key)
        return candidate.display if candidate is not None else ""

    def display_for_label(self, value: str) -> str:
        """把多封装计划中的原标签转换为候选库中完整的别名显示。"""

        key = self.unique_key_for_label(value)
        return self.display_for_key(key) if key else _clean_package_label(value)

    def _make_unique_key(self, base: str, drawing_code: str, pin_count: str) -> str:
        if base not in self.candidates:
            return base
        existing = self.candidates[base]
        if _candidate_is_compatible(
            existing,
            drawing_code=drawing_code,
            pin_count=pin_count,
        ):
            return base
        suffix = _normalize(drawing_code or pin_count or "variant")
        candidate = f"{base}|{suffix}"
        index = 2
        while candidate in self.candidates:
            candidate = f"{base}|{suffix}-{index}"
            index += 1
        return candidate


def resolve_document_packages(
    *,
    all_tables: Sequence[PackageTableSource],
    target_tables: Sequence[PackageTargetTable],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
) -> PackageResolutionResult:
    """先完成整篇文档的封装判断，再把结果交给逐行提取阶段。"""

    registry = build_document_package_registry(all_tables)

    # 多封装分析已经验证了列/行与 package 的结构关系，是最高优先级证据。
    for target in target_tables:
        plan = multi_package_plans.get(target.table_id)
        if plan is None or not plan.is_multi_package:
            continue
        for binding in plan.bindings:
            registry.register(
                binding.package,
                evidence=PackageEvidence(
                    "multi_package_plan",
                    target.table_id,
                    f"{plan.mode}: {binding.package}",
                    1.0,
                ),
            )

    assignments: dict[int, TablePackageAssignment] = {}
    for target in target_tables:
        assignments[target.table_id] = _resolve_direct_assignment(
            target,
            registry,
            multi_package_plans.get(target.table_id),
        )

    # 同一章节只有一个已经明确的封装时，未标注封装的后续表可以继承。
    _resolve_unique_section_assignments(target_tables, assignments)

    # 最后才使用引脚集合关联；该步骤只消费已经明确的归属，不依赖提取顺序。
    fingerprints = _build_package_fingerprints(
        target_tables,
        assignments,
        multi_package_plans,
        registry,
    )
    for target in target_tables:
        # 明确冲突属于已经完成的判断结果，不能再被弱一级的引脚集合关联
        # 覆盖；只有真正 unresolved 的表才进入最后关联阶段。
        if assignments[target.table_id].mode != "unresolved":
            continue
        overlap_assignment = _resolve_by_pin_overlap(target, fingerprints)
        # 即使没有得到 pkg，也要保留“证据不足”或“候选冲突”的明确原因，
        # 方便信息文件区分正常留空和算法没有执行。
        assignments[target.table_id] = overlap_assignment

    return PackageResolutionResult(registry, assignments)


def build_document_package_registry(
    tables: Sequence[PackageTableSource],
) -> PackageRegistry:
    """从所有表格建立候选库，包括最终不会进入引脚提取的封装信息表。"""

    registry = PackageRegistry()
    for table in tables:
        # 表题是表格局部证据；当前章节标题是作用域证据。上一章节不能用于
        # 当前表封装归属，因此不在这里登记为当前表证据。
        title_labels = _package_mentions_from_text(table.title)
        if title_labels:
            registry.register(
                title_labels[0],
                aliases=title_labels[1:],
                evidence=PackageEvidence("table_title", table.table_id, table.title, 0.98),
            )
        for heading in table.current_chapter_titles:
            heading_labels = _package_mentions_from_text(heading)
            if heading_labels:
                registry.register(
                    heading_labels[0],
                    aliases=heading_labels[1:],
                    evidence=PackageEvidence("current_heading", table.table_id, heading, 0.9),
                )
        _register_package_information_rows(registry, table)
    return registry


def assignment_to_debug(
    assignment: TablePackageAssignment,
    registry: PackageRegistry,
) -> dict[str, Any]:
    """把封装归属转换成可写入信息文件的普通字典。"""

    return {
        "packages": [registry.display_for_key(key) for key in assignment.package_keys],
        "package_keys": list(assignment.package_keys),
        "mode": assignment.mode,
        "confidence": assignment.confidence,
        "reason": assignment.reason,
    }


def _resolve_direct_assignment(
    target: PackageTargetTable,
    registry: PackageRegistry,
    plan: MultiPackagePlanLike | None,
) -> TablePackageAssignment:
    """按固定优先级处理一张表的明确局部封装证据。"""

    if plan is not None and plan.is_multi_package:
        keys = _unique_keys(
            registry.unique_key_for_label(binding.package)
            for binding in plan.bindings
        )
        return TablePackageAssignment(
            keys,
            mode=plan.mode,
            confidence=1.0,
            reason="多封装计划已明确绑定 package 与列/行",
        )

    if target.declared_package:
        declared_keys = registry.keys_for_label(target.declared_package)
        if len(declared_keys) > 1:
            return TablePackageAssignment(
                (),
                "conflict",
                0.0,
                "规则标题中的封装标签对应多个候选 drawing",
            )
        if len(declared_keys) == 1:
            return TablePackageAssignment(
                tuple(declared_keys),
                "rule_declared",
                1.0,
                "规则判断已从当前表题得到唯一封装",
            )
        key = registry.register(
            target.declared_package,
            evidence=PackageEvidence(
                "rule_declared",
                target.table_id,
                target.declared_package,
                1.0,
            ),
        )
        if key:
            return TablePackageAssignment(
                (key,),
                "rule_declared",
                1.0,
                "规则判断已从当前表题得到封装",
            )

    title_keys = registry.keys_for_table_sources(target.table_id, {"table_title"})
    title_keys.update(registry.all_keys_in_text(target.title))
    if len(title_keys) == 1:
        return TablePackageAssignment(
            tuple(title_keys),
            "table_title",
            0.98,
            "当前表格标题明确包含唯一封装",
        )
    if len(title_keys) > 1:
        return TablePackageAssignment(
            (),
            "conflict",
            0.0,
            "当前表格标题同时对应多个封装，保持未解析",
        )

    table_column_keys = registry.keys_for_table_sources(
        target.table_id,
        {"package_drawing_column", "package_name_column", "package_type_column"},
    )
    if len(table_column_keys) == 1:
        return TablePackageAssignment(
            tuple(table_column_keys),
            "table_package_column",
            0.95,
            "当前表的 package 字段只有一个封装值",
        )
    if len(table_column_keys) > 1:
        return TablePackageAssignment(
            (),
            "conflict",
            0.0,
            "当前表的 package 字段包含多个封装但没有多封装绑定",
        )

    heading_keys: set[str] = set()
    for heading in target.current_chapter_titles:
        heading_keys.update(registry.all_keys_in_text(heading))
        for label in _package_mentions_from_text(heading):
            key = registry.unique_key_for_label(label)
            if key:
                heading_keys.add(key)
    if len(heading_keys) == 1:
        return TablePackageAssignment(
            tuple(heading_keys),
            "current_chapter",
            0.9,
            "当前章节上下文只包含一个明确封装",
        )
    if len(heading_keys) > 1:
        return TablePackageAssignment(
            (),
            "conflict",
            0.0,
            "当前章节同时包含多个封装，不能直接继承",
        )

    return TablePackageAssignment(reason="未发现表格局部封装证据")


def _resolve_unique_section_assignments(
    targets: Sequence[PackageTargetTable],
    assignments: dict[int, TablePackageAssignment],
) -> None:
    """同一最深章节只有一个明确封装时，为未标注表继承该封装。"""

    scope_packages: dict[str, set[str]] = {}
    for target in targets:
        assignment = assignments[target.table_id]
        if len(assignment.package_keys) != 1:
            continue
        scope = _section_scope(target.current_chapter_titles)
        if scope:
            scope_packages.setdefault(scope, set()).update(assignment.package_keys)

    for target in targets:
        if assignments[target.table_id].package_keys:
            continue
        scope = _section_scope(target.current_chapter_titles)
        keys = scope_packages.get(scope, set())
        if scope and len(keys) == 1:
            assignments[target.table_id] = TablePackageAssignment(
                tuple(keys),
                "same_section",
                0.82,
                "同一最深章节中的明确目标表只属于一个封装",
            )


def _build_package_fingerprints(
    targets: Sequence[PackageTargetTable],
    assignments: Mapping[int, TablePackageAssignment],
    plans: Mapping[int, MultiPackagePlanLike],
    registry: PackageRegistry,
) -> dict[str, _PackageFingerprint]:
    """汇总已经明确归属表的 pin_no/pin_name，结果与表格处理顺序无关。"""

    fingerprints: dict[str, _PackageFingerprint] = {}
    for target in targets:
        plan = plans.get(target.table_id)
        if plan is not None and plan.is_multi_package:
            for binding in plan.bindings:
                # 多封装计划的 binding.package 已在候选库登记。直接通过
                # 唯一别名查询其 key，不能依赖 binding 顺序或从 key 反推别名。
                matching_key = registry.unique_key_for_label(binding.package)
                if not matching_key:
                    continue
                pin_numbers, pin_names = _collect_binding_fingerprint(target, binding)
                profile = fingerprints.setdefault(matching_key, _PackageFingerprint())
                profile.pin_numbers.update(pin_numbers)
                profile.pin_names.update(pin_names)
            continue

        assignment = assignments[target.table_id]
        if len(assignment.package_keys) != 1:
            continue
        pin_numbers, pin_names = _collect_target_fingerprint(target)
        profile = fingerprints.setdefault(assignment.package_keys[0], _PackageFingerprint())
        profile.pin_numbers.update(pin_numbers)
        profile.pin_names.update(pin_names)
    return fingerprints


def _collect_binding_fingerprint(
    target: PackageTargetTable,
    binding: PackageBindingLike,
) -> tuple[set[str], set[str]]:
    pin_numbers: set[str] = set()
    pin_names: set[str] = set()
    for row_index, row in enumerate(target.data_rows):
        if binding.row_indexes is not None and row_index not in binding.row_indexes:
            continue
        pin_numbers.update(_pin_tokens(_cell(row, binding.pin_no_column)))
        if binding.pin_name_column is not None:
            pin_names.update(_pin_name_tokens(_cell(row, binding.pin_name_column)))
    return pin_numbers, pin_names


def _collect_target_fingerprint(
    target: PackageTargetTable,
) -> tuple[set[str], set[str]]:
    pin_indexes = [
        column.index
        for column in target.columns
        if _normalize_field_name(column.field_name) == "pin_no"
    ]
    name_indexes = [
        column.index
        for column in target.columns
        if _normalize_field_name(column.field_name) == "pin_name"
    ]
    pin_numbers: set[str] = set()
    pin_names: set[str] = set()
    for row_index, row in enumerate(target.data_rows):
        if (
            target.included_row_indexes is not None
            and row_index not in target.included_row_indexes
        ):
            continue
        for index in pin_indexes:
            pin_numbers.update(_pin_tokens(_cell(row, index)))
        for index in name_indexes:
            pin_names.update(_pin_name_tokens(_cell(row, index)))
    return pin_numbers, pin_names


def _resolve_by_pin_overlap(
    target: PackageTargetTable,
    fingerprints: Mapping[str, _PackageFingerprint],
) -> TablePackageAssignment:
    """使用编号和名称的联合重合度关联；仅数字编号不能单独决定封装。"""

    current_pins, current_names = _collect_target_fingerprint(target)
    if len(current_pins) < 2 and len(current_names) < 3:
        return TablePackageAssignment(reason="当前表引脚证据不足")

    scored: list[tuple[float, str, str]] = []
    for key, profile in fingerprints.items():
        pin_ratio = _overlap_ratio(current_pins, profile.pin_numbers)
        name_ratio = _overlap_ratio(current_names, profile.pin_names)
        if current_pins and current_names:
            score = pin_ratio * 0.6 + name_ratio * 0.4
            valid = pin_ratio >= 0.5 and name_ratio >= 0.5
        elif len(current_names) >= 3:
            score = name_ratio * 0.9
            valid = name_ratio >= 0.85
        else:
            # 只有编号时容易把多个 1..N 数字封装误判为同一个封装。
            score = 0.0
            valid = False
        if valid:
            scored.append((score, key, f"pin={pin_ratio:.2f}, name={name_ratio:.2f}"))

    if not scored:
        return TablePackageAssignment(reason="没有封装达到引脚集合关联阈值")
    scored.sort(reverse=True)
    best_score, best_key, detail = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < 0.72 or best_score - second_score < 0.15:
        return TablePackageAssignment(
            (),
            "overlap_conflict",
            0.0,
            "引脚集合关联结果不唯一",
        )
    return TablePackageAssignment(
        (best_key,),
        "pin_overlap",
        best_score,
        f"引脚集合唯一匹配：{detail}",
    )


def _register_package_information_rows(
    registry: PackageRegistry,
    table: PackageTableSource,
) -> None:
    """从 Package Drawing/Type/Name 等明确字段中读取封装实体。"""

    rows = table.rows
    for header_index, header_row in enumerate(rows[:8]):
        roles = [_package_header_role(cell) for cell in header_row]
        package_indexes = [index for index, role in enumerate(roles) if role]
        if not package_indexes:
            continue
        # Package Quantity、Eco Plan 等非封装身份字段不会产生 role；这里至少
        # 需要 drawing/name/type/package 之一才能把后续行当作封装信息。
        if not any(roles[index] in {"drawing", "name", "type", "package"} for index in package_indexes):
            continue
        pin_count_index = next(
            (index for index, role in enumerate(roles) if role == "pin_count"),
            None,
        )
        for row in rows[header_index + 1 : header_index + 201]:
            values_by_role: dict[str, list[str]] = {}
            for index in package_indexes:
                value = _cell(row, index)
                if _looks_like_package_value(value):
                    values_by_role.setdefault(roles[index], []).append(value)
            labels = [
                *values_by_role.get("drawing", []),
                *values_by_role.get("name", []),
                *values_by_role.get("type", []),
                *values_by_role.get("package", []),
            ]
            labels = _unique_labels(labels)
            if not labels:
                continue
            pin_count = _extract_pin_count(
                _cell(row, pin_count_index) if pin_count_index is not None else ""
            )
            drawing = next(iter(values_by_role.get("drawing", [])), "")
            family = next(iter(values_by_role.get("type", [])), "")
            source = (
                "package_drawing_column"
                if drawing
                else "package_name_column"
                if values_by_role.get("name") or values_by_role.get("package")
                else "package_type_column"
            )
            registry.register(
                labels[0],
                aliases=labels[1:],
                drawing_code=drawing,
                family=family,
                pin_count=pin_count,
                evidence=PackageEvidence(
                    source,
                    table.table_id,
                    " | ".join(labels),
                    0.97 if drawing else 0.9,
                ),
            )
        # 同一张表只采用最早命中的明确 package 表头，防止把数据中的重复
        # 文本误当成第二套表头再次扫描。
        return


def _package_mentions_from_text(value: str) -> list[str]:
    """只在 Package/封装明确语境中抽取名称，不扫描普通大写词。"""

    text = _clean_text(value)
    if not text or len(text) > 320:
        return []
    mentions: list[str] = []
    patterns = (
        r"\bpackage\s+(?:drawing|code|name|type)\s*[:：#-]?\s*([A-Za-z0-9][A-Za-z0-9_-]{1,30})",
        r"\b([A-Za-z][A-Za-z0-9_-]{1,30})\s+packages?\b",
        r"[（(]\s*([A-Za-z][A-Za-z0-9_-]{1,30})\s+packages?\s*[）)]",
        r"\b([A-Za-z][A-Za-z0-9_-]{1,30})\s*封装\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            label = _clean_package_label(match.group(1))
            if _looks_like_package_value(label):
                mentions.append(label)

    # “64-Pin QFN Package”和“QFN 64 Pin”同时保留家族与 pin count。
    for match in re.finditer(
        r"\b(\d{2,4}\s*[- ]?\s*(?:pin|ball)\s+[A-Za-z][A-Za-z0-9_-]{1,20})",
        text,
        flags=re.IGNORECASE,
    ):
        mentions.append(_clean_package_label(match.group(1)))
    return _unique_labels(mentions)


def _derive_label_metadata(
    labels: Sequence[str],
    drawing_code: str,
    family: str,
    pin_count: str,
) -> dict[str, Any]:
    """从 ZCE-64、64 Pin QFN 等别名补充 code/family/pin count。"""

    aliases = _unique_labels(labels)
    primary = _clean_package_label(drawing_code) or aliases[0]
    drawing_code = _clean_package_label(drawing_code)
    family = _clean_package_label(family)
    pin_count = _extract_pin_count(pin_count)

    for label in list(aliases):
        if not pin_count:
            pin_count = _extract_pin_count(label)
        code_count = re.fullmatch(r"([A-Za-z]{2,10})[- ](\d{2,4})", label)
        if code_count:
            base_code = code_count.group(1).upper()
            if base_code not in aliases:
                aliases.append(base_code)
            if not pin_count:
                pin_count = code_count.group(2)
        family_match = re.search(
            r"(?:\d{2,4}\s*[- ]?\s*(?:pin|ball)\s+)([A-Za-z][A-Za-z0-9_-]{1,20})",
            label,
            flags=re.IGNORECASE,
        )
        if family_match and not family:
            family = family_match.group(1).upper()
            if family not in aliases:
                aliases.append(family)

    return {
        "primary": primary,
        "aliases": aliases,
        "drawing_code": drawing_code,
        "family": family,
        "pin_count": pin_count,
    }


def _candidate_key(primary: str, drawing_code: str, family: str, pin_count: str) -> str:
    if drawing_code:
        return f"drawing={_normalize(drawing_code)}"
    base = _normalize(primary or family)
    suffix = f"|pins={pin_count}" if pin_count else ""
    return f"label={base or 'unknown'}{suffix}"


def _candidate_is_compatible(
    candidate: PackageCandidate,
    *,
    drawing_code: str,
    pin_count: str,
) -> bool:
    if candidate.drawing_code and drawing_code:
        if _normalize(candidate.drawing_code) != _normalize(drawing_code):
            return False
    if candidate.pin_count and pin_count and candidate.pin_count != pin_count:
        return False
    return True


def _package_header_role(value: str) -> str:
    text = _normalize(value)
    if not text:
        return ""
    if "package drawing" in text or "drawing code" in text or "封装图" in text:
        return "drawing"
    if "package name" in text or "package code" in text or "封装名称" in text:
        return "name"
    if "package type" in text or "封装类型" in text:
        return "type"
    if text in {"package", "pkg", "封装"}:
        return "package"
    if text in {"pins", "pin count", "number of pins", "terminal count", "引脚数"}:
        return "pin_count"
    return ""


def _looks_like_package_value(value: str) -> bool:
    text = _clean_package_label(value)
    if not text or len(text) > 80:
        return False
    normalized = _normalize(text)
    invalid = {
        "package",
        "packages",
        "pkg",
        "package drawing",
        "package name",
        "package type",
        "drawing",
        "name",
        "type",
        "device",
        "mode",
        "pin",
        "ball",
        "signal",
        "table",
        "orderable part number",
        "n a",
        "na",
        "none",
        "yes",
        "no",
    }
    if normalized in invalid or re.fullmatch(r"\d+", normalized):
        return False
    return bool(re.search(r"[A-Za-z\u4e00-\u9fff]", text))


def _extract_pin_count(value: str) -> str:
    text = _clean_text(value)
    match = re.search(r"\b(\d{2,4})\s*[- ]?\s*(?:pin|pins|ball|balls)\b", text, re.IGNORECASE)
    if not match:
        match = re.fullmatch(r"\s*(\d{2,4})\s*", text)
    return match.group(1) if match else ""


def _section_scope(headings: Sequence[str]) -> str:
    """使用最深的编号章节作为继承边界，避免整章范围过宽。"""

    numbered = []
    for heading in headings:
        text = _clean_text(heading)
        if re.match(r"^\d+(?:\.\d+)*\b", text):
            numbered.append(text)
    if numbered:
        return _normalize(numbered[-1])
    return _normalize(headings[-1]) if headings else ""


def _pin_tokens(value: str) -> set[str]:
    text = _clean_text(value).upper()
    tokens = set(re.findall(r"(?<![A-Z0-9])[A-Z]{1,4}\d{1,4}(?![A-Z0-9])", text))
    tokens.update(re.findall(r"(?<![A-Z0-9])\d{1,4}(?![A-Z0-9])", text))
    return tokens


def _pin_name_tokens(value: str) -> set[str]:
    text = _clean_text(value).upper()
    result = set()
    for part in re.split(r"[\n,;/|]+", text):
        normalized = re.sub(r"\s+", " ", part).strip()
        if normalized and normalized not in {"RESERVED", "N/A", "NA"}:
            result.add(normalized)
    return result


def _overlap_ratio(current: set[str], known: set[str]) -> float:
    if not current or not known:
        return 0.0
    return len(current & known) / max(1, len(current))


def _normalize_field_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    aliases = {
        "ball_no": "pin_no",
        "terminal_no": "pin_no",
        "package_pin_no": "pin_no",
        "ball_name": "pin_name",
        "signal_name": "pin_name",
        "terminal_name": "pin_name",
        "pad_name": "pin_name",
    }
    return aliases.get(normalized, normalized)


def _unique_labels(values: Sequence[str]) -> list[str]:
    result = []
    normalized_seen = set()
    for value in values:
        cleaned = _clean_package_label(value)
        normalized = _normalize(cleaned)
        if cleaned and normalized and normalized not in normalized_seen:
            result.append(cleaned)
            normalized_seen.add(normalized)
    return result


def _unique_keys(values: Iterable[str]) -> tuple[str, ...]:
    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return tuple(result)


def _clean_package_label(value: str) -> str:
    text = _clean_text(value)
    text = re.sub(r"\[[^]]+\]", "", text)
    text = re.sub(r"\bpackages?\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" -:/,;&()（）")


def _clean_text(value: str) -> str:
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def _normalize(value: str) -> str:
    text = _clean_text(value).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _cell(row: Sequence[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return str(row[index]).strip()
