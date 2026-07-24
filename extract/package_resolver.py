"""识别文档中的封装，并在逐行提取前完成表格封装归属。

本模块只处理输出 JSON 最外层的 ``pkg``，不参与以下工作：

* 不判断表格是否需要提取；
* 不判断或修改 ``pin_no``、``pin_name``、``type`` 字段；
* 不生成引脚记录；
* 不修改表格的 ``group``；
* 不修改字段判断模型的职责。

处理流程固定为六个阶段：

1. 把最终 Markdown 按原始顺序拆成标题、正文和完整表格块，每个块分配
   稳定 ``block_id``，作为后续模型输出必须引用的证据。
2. 分块读取全文，发现具体封装实体。模型只能返回原文中真实出现的名称和
   证据块 ID；代码会逐项校验，无法在证据中找到的名称一律拒绝。
3. 接收已经完成的多封装计划。多个封装专属编号列、package 控制列和
   package 分段行属于最强证据，直接登记为多封装归属。
4. 单封装表严格按“当前表题 > 当前表头 > 邻近上下文 > 当前章节 >
   续表继承”确定 pkg。表题/表头中的具体封装优先于正文中的弱证据；
   两者出现互相冲突的具体封装时保持未解析，不能猜测。
5. 对仍未归属的表，模型只能从已经验证的候选 ``package_id`` 中选择，并
   必须返回有效证据块 ID，不能创造新的封装名称。
6. 最后使用已明确封装表的引脚编号/名称集合做保守关联；证据冲突
   或分数不足时保持空字符串，不能为了减少空 pkg 而猜测。

封装实体按具体名称、package drawing/code、封装家族、pin count 和器件作用
域组织。封装家族（例如 QFN、BGA）不是具体 pkg；相同字段名也不构成封装
相同证据。不同 drawing code 或不同器件作用域不能仅因名称相似而合并。

单封装表出现多个候选时，当前表题中的具体 pkg 优先，其次是当前表头。
真正的多封装表不执行“选择一个”的规则，继续保留多封装模块已经建立的
全部 package 与 pin 列/行绑定。

候选实体可以在内部保留多个别名，但最终 ``pkg`` 永远只能是一个名称：

* 当前表题命中的原始名称优先于当前表头，表头优先于其他上下文；
* 没有表级名称时，优先使用明确的 package drawing/code，再使用候选实体
  中得分最高的具体短名称；
* ``|`` 只允许作为内部 key 的组成字符，绝不能出现在最终 ``pkg``；
* 多个不同封装不能拼成一个字符串，必须由多封装分支生成多个外层对象；
* 名称通常不超过 15 个字符，短名称是优先信号而不是截断规则。证据充分的
  长名称可以保留，代码不能擅自截断或改写原文。
"""

from __future__ import annotations

import html as html_lib
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from extract.semantic_classifier import call_model_json


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
class PackageDocumentBlock:
    """最终 Markdown 中一个可引用的有序证据块。"""

    block_id: str
    order: int
    block_type: str
    text: str
    table_id: int | None = None
    heading_path: tuple[str, ...] = ()


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
    block_id: str = ""


@dataclass
class PackageCandidate:
    """文档中一个规范化后的封装实体及其别名。"""

    key: str
    aliases: list[str] = field(default_factory=list)
    drawing_code: str = ""
    family: str = ""
    pin_count: str = ""
    role: str = "package_identity"
    device_scope: str = ""
    evidence: list[PackageEvidence] = field(default_factory=list)
    # alias_priorities 只参与内部规范名称选择，不写入最终 JSON。字典的 key
    # 使用清洗后的原始名称，value 越大表示证据来源越直接、名称越具体。
    alias_priorities: dict[str, tuple[int, ...]] = field(default_factory=dict)
    canonical_name: str = ""

    @property
    def display(self) -> str:
        """返回一个规范名称；别名只作内部证据，不能用 ``|`` 拼接输出。"""

        return _clean_output_package_label(self.canonical_name)


@dataclass(frozen=True)
class TablePackageAssignment:
    """一张目标表在行提取前得到的封装归属。"""

    package_keys: tuple[str, ...] = ()
    mode: str = "unresolved"
    confidence: float = 0.0
    reason: str = ""
    # selected_label 保存当前表题/表头实际命中的名称。它只属于当前表，
    # 不能反向覆盖候选实体中其他表格使用的别名。
    selected_label: str = ""


@dataclass
class PackageResolutionResult:
    """整篇文档的候选库和所有目标表归属。"""

    registry: "PackageRegistry"
    assignments: dict[int, TablePackageAssignment]
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def package_label(self, table_id: int) -> str:
        """返回单封装表的显示名称；多封装表由原绑定计划逐行读取。"""

        assignment = self.assignments.get(table_id)
        if assignment is None or len(assignment.package_keys) != 1:
            return ""
        selected = _clean_output_package_label(assignment.selected_label)
        if selected:
            return selected
        return self.registry.display_for_key(assignment.package_keys[0])

    def package_key(self, table_id: int) -> str:
        """返回单封装表唯一的内部 package key，供最终分组保持身份边界。"""

        assignment = self.assignments.get(table_id)
        if assignment is None or len(assignment.package_keys) != 1:
            return ""
        return assignment.package_keys[0]

    def package_priority(self, table_id: int) -> int:
        """返回当前表 pkg 证据等级，供同一实体出现别名时选择显示名称。"""

        assignment = self.assignments.get(table_id)
        if assignment is None:
            return 0
        return {
            "multi_package_columns": 110,
            "multi_package_control_column": 110,
            "multi_package_sections": 110,
            "table_title": 100,
            "table_header": 90,
            "rule_declared": 85,
            "semantic_context": 70,
            "continuation": 65,
            "current_chapter": 60,
            "same_section": 55,
            "pin_overlap": 40,
        }.get(assignment.mode, 0)


@dataclass
class _PackageFingerprint:
    """已明确封装的引脚集合，只用于后续未知表的保守关联。"""

    pin_numbers: set[str] = field(default_factory=set)
    pin_names: set[str] = field(default_factory=set)


PackageEntityClassifier = Callable[
    [Sequence[PackageDocumentBlock]],
    Mapping[str, Any],
]
PackageAssignmentClassifier = Callable[
    [
        PackageTargetTable,
        Sequence[PackageDocumentBlock],
        Sequence[PackageCandidate],
    ],
    Mapping[str, Any],
]


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
        role: str = "package_identity",
        device_scope: str = "",
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
        device_scope = _clean_package_label(device_scope)
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
                device_scope=device_scope,
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
            key = _candidate_key(
                primary_label,
                drawing_code,
                family,
                pin_count,
                device_scope,
            )
            key = self._make_unique_key(
                key,
                drawing_code,
                pin_count,
                device_scope,
            )
            candidate = self.candidates.setdefault(
                key,
                PackageCandidate(
                    key=key,
                    drawing_code=drawing_code,
                    family=family,
                    pin_count=pin_count,
                    role=role,
                    device_scope=device_scope,
                ),
            )

        if not candidate.drawing_code:
            candidate.drawing_code = drawing_code
        if not candidate.family:
            candidate.family = family
        if not candidate.pin_count:
            candidate.pin_count = pin_count
        if not candidate.device_scope:
            candidate.device_scope = device_scope
        if candidate.role != "package_identity" and role == "package_identity":
            candidate.role = role
        for label in cleaned_labels:
            if label not in candidate.aliases:
                candidate.aliases.append(label)
            self._alias_to_keys.setdefault(_normalize(label), set()).add(candidate.key)
            # 每次登记都更新该别名的证据等级。这里不删除旧别名，只重新计算
            # 哪一个名称可以作为该实体在缺少表级命中时的规范显示名称。
            priority = _package_output_label_priority(
                label,
                source=evidence.source,
                drawing_code=drawing_code,
                is_primary=_normalize(label) == _normalize(primary_label),
            )
            previous = candidate.alias_priorities.get(label)
            if previous is None or priority > previous:
                candidate.alias_priorities[label] = priority
        candidate.canonical_name = _select_candidate_canonical_name(candidate)
        # family 可用于识别“QFN 同时对应多个 drawing”的歧义，但不追加到
        # 最终 pkg 显示别名，也不能作为具体身份命中表题/表头。
        if family:
            self._alias_to_keys.setdefault(_normalize(family), set()).add(candidate.key)
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

    def specific_keys_in_text(self, value: str) -> set[str]:
        """只通过候选的具体身份名称匹配文本，忽略 family 别名。"""

        normalized_text = _normalize(value)
        result: set[str] = set()
        for key, candidate in self.candidates.items():
            if candidate.role != "package_identity":
                continue
            generic_aliases = {
                _normalize(candidate.family),
                *(
                    _normalize(alias)
                    for alias in candidate.aliases
                    if _package_label_role(alias) == "package_family"
                ),
            }
            for alias in candidate.aliases:
                normalized_alias = _normalize(alias)
                if (
                    not normalized_alias
                    or normalized_alias in generic_aliases
                    or len(normalized_alias) < 2
                ):
                    continue
                if re.search(
                    rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])",
                    normalized_text,
                ):
                    result.add(key)
                    break
        return result

    def best_label_for_key_in_text(self, key: str, value: str) -> str:
        """返回文本中属于指定实体的最佳原始别名，不从其他上下文猜名称。"""

        candidate = self.candidates.get(key)
        if candidate is None:
            return ""
        matching = [
            alias
            for alias in candidate.aliases
            if _is_specific_output_package_label(alias)
            and _text_contains_exact_label(value, alias)
        ]
        if not matching:
            return ""
        return max(
            matching,
            key=lambda alias: candidate.alias_priorities.get(
                alias,
                _package_output_label_priority(alias),
            ),
        )

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
        """返回多封装绑定中的一个原始名称，绝不扩展成别名拼接字符串。"""

        cleaned = _clean_output_package_label(value)
        key = self.unique_key_for_label(value)
        if key:
            candidate = self.candidates.get(key)
            # QFN、SSOP 等文本单独出现时只是机械家族，但若多封装结构分析
            # 已把它绑定为一条独立 pin_no 列，它就是该列必须保留的 pkg。
            # 这种结构证据优先于通用的“家族不能作具体身份”过滤规则。
            is_multi_package_binding = bool(
                candidate
                and any(
                    evidence.source == "multi_package_plan"
                    for evidence in candidate.evidence
                )
            )
            if cleaned and (
                _is_specific_output_package_label(cleaned)
                or is_multi_package_binding
            ):
                return cleaned
        return self.display_for_key(key) if key else cleaned

    def _make_unique_key(
        self,
        base: str,
        drawing_code: str,
        pin_count: str,
        device_scope: str,
    ) -> str:
        if base not in self.candidates:
            return base
        existing = self.candidates[base]
        if _candidate_is_compatible(
            existing,
            drawing_code=drawing_code,
            pin_count=pin_count,
            device_scope=device_scope,
        ):
            return base
        suffix = _normalize(drawing_code or pin_count or "variant")
        candidate = f"{base}|{suffix}"
        index = 2
        while candidate in self.candidates:
            candidate = f"{base}|{suffix}-{index}"
            index += 1
        return candidate

    def specific_keys(self, keys: Iterable[str]) -> set[str]:
        """只保留可以写入最终 ``pkg`` 的具体封装实体。"""

        return {
            key
            for key in keys
            if key in self.candidates
            and self.candidates[key].role == "package_identity"
        }


def resolve_document_packages(
    *,
    all_tables: Sequence[PackageTableSource],
    target_tables: Sequence[PackageTargetTable],
    multi_package_plans: Mapping[int, MultiPackagePlanLike],
    document_blocks: Sequence[PackageDocumentBlock] = (),
    use_semantic_classifier: bool = False,
    entity_classifier: PackageEntityClassifier | None = None,
    assignment_classifier: PackageAssignmentClassifier | None = None,
) -> PackageResolutionResult:
    """先完成整篇文档的封装判断，再把结果交给逐行提取阶段。"""

    registry = build_document_package_registry(all_tables)
    diagnostics: list[dict[str, Any]] = []
    blocks = tuple(document_blocks) or build_package_document_blocks_from_tables(all_tables)

    # 封装语义模型与表格字段模型是两个独立任务。这里扫描全文，只建立
    # 可验证的封装实体，不决定 pin_no/pin_name/type，也不生成引脚记录。
    if use_semantic_classifier or entity_classifier is not None:
        diagnostics.extend(
            discover_document_package_entities(
                blocks,
                registry,
                classifier=entity_classifier,
            )
        )

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

    # 局部表题/表头没有答案时，再允许语义模型从已验证的 package_id 中
    # 选择。模型不能返回自由文本封装名，也不能覆盖前面的局部冲突。
    if use_semantic_classifier or assignment_classifier is not None:
        diagnostics.extend(
            _resolve_semantic_assignments(
                target_tables,
                assignments,
                registry,
                blocks,
                classifier=assignment_classifier,
            )
        )

    # 续表继承只接受规范化后完全相同的 Table 编号/标题。
    _resolve_continuation_assignments(target_tables, assignments)

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

    return PackageResolutionResult(registry, assignments, diagnostics)


def build_package_document_blocks_from_markdown(
    markdown: str,
) -> list[PackageDocumentBlock]:
    """把最终 Markdown 转换为可供封装语义判断引用的有序证据块。

    表格保留完整文本并带上 ``table_id``；普通正文按自然段保留。模型看到
    的是整篇文档的全部块，只是在网络请求时按块边界分批发送。
    """

    blocks: list[PackageDocumentBlock] = []
    heading_stack: list[str] = []
    table_pattern = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
    cursor = 0
    table_id = 0

    def append_text_blocks(fragment: str) -> None:
        nonlocal heading_stack
        for paragraph in re.split(r"\n\s*\n", fragment):
            text = _clean_markdown_text(paragraph)
            if not text:
                continue
            heading_match = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", paragraph.strip())
            block_type = "heading" if heading_match else "paragraph"
            if heading_match:
                level = len(heading_match.group(1))
                heading_text = _clean_markdown_text(heading_match.group(2))
                heading_stack = heading_stack[: level - 1]
                heading_stack.append(heading_text)
                text = heading_text
            blocks.append(
                PackageDocumentBlock(
                    block_id=f"block-{len(blocks)}",
                    order=len(blocks),
                    block_type=block_type,
                    text=text,
                    heading_path=tuple(heading_stack),
                )
            )

    for match in table_pattern.finditer(markdown):
        append_text_blocks(markdown[cursor : match.start()])
        table_text = _clean_text(match.group(0))
        blocks.append(
            PackageDocumentBlock(
                block_id=f"block-{len(blocks)}",
                order=len(blocks),
                block_type="table",
                text=table_text,
                table_id=table_id,
                heading_path=tuple(heading_stack),
            )
        )
        table_id += 1
        cursor = match.end()
    append_text_blocks(markdown[cursor:])
    return blocks


def build_package_document_blocks_from_tables(
    tables: Sequence[PackageTableSource],
) -> list[PackageDocumentBlock]:
    """为 middle_json 和单元测试构造只含表格的兼容证据流。"""

    blocks: list[PackageDocumentBlock] = []
    for table in tables:
        text = "\n".join(
            part
            for part in (
                table.title,
                *(" | ".join(row) for row in table.rows),
            )
            if part
        )
        blocks.append(
            PackageDocumentBlock(
                block_id=f"table-{table.table_id}",
                order=len(blocks),
                block_type="table",
                text=text,
                table_id=table.table_id,
                heading_path=table.current_chapter_titles,
            )
        )
    return blocks


def discover_document_package_entities(
    blocks: Sequence[PackageDocumentBlock],
    registry: PackageRegistry,
    *,
    classifier: PackageEntityClassifier | None = None,
) -> list[dict[str, Any]]:
    """分块发现具体封装实体，并拒绝没有原文证据的模型结果。"""

    diagnostics: list[dict[str, Any]] = []
    chunks = _chunk_document_blocks(blocks)
    if not chunks:
        return diagnostics

    classify = classifier or _classify_package_entity_chunk
    workers = max(
        1,
        int(
            os.getenv(
                "EXTRACT_PACKAGE_WORKERS",
                os.getenv("EXTRACT_SCHEMA_WORKERS", "4"),
            )
        ),
    )
    print(
        f"封装语义发现: 文档块 {len(blocks)} 个, 分块 {len(chunks)} 个, "
        f"并发 {min(workers, len(chunks))}"
    )
    responses: list[tuple[int, Sequence[PackageDocumentBlock], Mapping[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(chunks))) as executor:
        futures = {
            executor.submit(classify, chunk): (index, chunk)
            for index, chunk in enumerate(chunks)
        }
        completed = 0
        for future in as_completed(futures):
            index, chunk = futures[future]
            try:
                responses.append((index, chunk, future.result()))
            except Exception as exc:
                diagnostics.append(
                    {
                        "stage": "package_entity_discovery",
                        "chunk_index": index,
                        "status": "error",
                        "reason": str(exc),
                    }
                )
            completed += 1
            print(f"封装语义发现进度: {completed}/{len(chunks)}")

    # 并发只影响请求速度；按原 chunk 顺序登记，保证别名显示和调试稳定。
    for index, chunk, response in sorted(responses, key=lambda item: item[0]):
        block_map = {block.block_id: block for block in chunk}
        raw_entities = response.get("entities", []) if isinstance(response, Mapping) else []
        if not isinstance(raw_entities, list):
            raw_entities = []
        for raw_entity in raw_entities:
            validated, reason = _validate_semantic_entity(raw_entity, block_map)
            if validated is None:
                diagnostics.append(
                    {
                        "stage": "package_entity_discovery",
                        "chunk_index": index,
                        "status": "rejected",
                        "reason": reason,
                        "raw": raw_entity,
                    }
                )
                continue
            evidence_ids = validated["evidence_block_ids"]
            primary_block = block_map[evidence_ids[0]]
            key = registry.register(
                validated["name"],
                aliases=validated["aliases"],
                drawing_code=validated["drawing_code"],
                family=validated["family"],
                pin_count=validated["pin_count"],
                role="package_identity",
                device_scope=validated["device_scope"],
                evidence=PackageEvidence(
                    "semantic_document",
                    primary_block.table_id
                    if primary_block.table_id is not None
                    else -1,
                    " | ".join(evidence_ids),
                    0.94,
                    block_id=evidence_ids[0],
                ),
            )
            diagnostics.append(
                {
                    "stage": "package_entity_discovery",
                    "chunk_index": index,
                    "status": "accepted" if key else "rejected",
                    "package_key": key,
                    "name": validated["name"],
                    "evidence_block_ids": evidence_ids,
                    "reason": "" if key else "候选与已有实体存在不可消除的歧义",
                }
            )
    return diagnostics


def _classify_package_entity_chunk(
    blocks: Sequence[PackageDocumentBlock],
) -> Mapping[str, Any]:
    """调用模型，只发现封装实体，不为具体表格做归属。"""

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("封装语义判断需要环境变量 DEEPSEEK_API_KEY")
    payload = {
        "task": "Find concrete physical package identities in these ordered datasheet blocks.",
        "rules": [
            "Read every supplied block; do not ignore a block because it lacks the word Package.",
            "Return only package identities that control a physical pin/ball mapping.",
            "QFN, BGA, LQFP, SOP and similar mechanical families alone are package_family, not package_identity.",
            "Device model names and orderable part numbers are not package identities.",
            "Keep different device scopes or different package drawing codes as different entities.",
            "Treat two names as aliases only when the source explicitly states equivalence or one is an explicit code/pin-count variant of the same package.",
            "Copy name and aliases exactly from evidence text. Never invent or translate a name.",
            "Every entity must cite one or more supplied block_id values.",
        ],
        "blocks": [_block_to_payload(block) for block in blocks],
        "output_schema": {
            "entities": [
                {
                    "name": "exact source text",
                    "role": "package_identity|package_family|device_model",
                    "aliases": ["exact explicit alias"],
                    "family": "optional exact family text",
                    "drawing_code": "optional exact drawing/code",
                    "pin_count": "optional digits",
                    "device_scope": "optional device/model scope",
                    "evidence_block_ids": ["block-id"],
                }
            ]
        },
    }
    return call_model_json(
        payload,
        api_key=api_key,
        system_prompt=(
            "You identify concrete semiconductor package identities from complete ordered "
            "datasheet blocks. Return JSON containing only entities. Every name must be copied "
            "from cited evidence. Do not decide table columns or generate pin records."
        ),
        max_tokens=int(os.getenv("EXTRACT_PACKAGE_ENTITY_MAX_TOKENS", "6000")),
        timeout=float(os.getenv("EXTRACT_PACKAGE_TIMEOUT", "60")),
    )


def _validate_semantic_entity(
    raw_entity: Any,
    block_map: Mapping[str, PackageDocumentBlock],
) -> tuple[dict[str, Any] | None, str]:
    """验证模型实体的类型、证据 ID 和原文可追溯性。"""

    if not isinstance(raw_entity, Mapping):
        return None, "实体不是对象"
    if str(raw_entity.get("role") or "").strip() != "package_identity":
        return None, "不是具体 package_identity"
    name = _clean_package_label(str(raw_entity.get("name") or ""))
    evidence_ids = _unique_strings(raw_entity.get("evidence_block_ids"))
    if not name:
        return None, "封装名称为空"
    if _package_label_role(name) == "package_family":
        return None, "只识别到机械封装家族，不是具体 package_identity"
    if not evidence_ids or any(block_id not in block_map for block_id in evidence_ids):
        return None, "证据块 ID 缺失或无效"
    evidence_text = "\n".join(block_map[block_id].text for block_id in evidence_ids)
    if not _text_contains_exact_label(evidence_text, name):
        return None, "封装名称未出现在证据原文中"

    aliases = []
    for alias in _unique_strings(raw_entity.get("aliases")):
        cleaned = _clean_package_label(alias)
        if (
            cleaned
            and _package_label_role(cleaned) == "package_identity"
            and _text_contains_exact_label(evidence_text, cleaned)
        ):
            aliases.append(cleaned)
    return (
        {
            "name": name,
            "aliases": aliases,
            "family": _clean_package_label(str(raw_entity.get("family") or "")),
            "drawing_code": _clean_package_label(
                str(raw_entity.get("drawing_code") or "")
            ),
            "pin_count": _extract_pin_count(str(raw_entity.get("pin_count") or "")),
            "device_scope": _clean_package_label(
                str(raw_entity.get("device_scope") or "")
            ),
            "evidence_block_ids": evidence_ids,
        },
        "",
    )


def build_document_package_registry(
    tables: Sequence[PackageTableSource],
) -> PackageRegistry:
    """从所有表格建立候选库，包括最终不会进入引脚提取的封装信息表。"""

    registry = PackageRegistry()
    for table in tables:
        # 表题是表格局部证据；当前章节标题是作用域证据。上一章节不能用于
        # 当前表封装归属，因此不在这里登记为当前表证据。
        title_labels = _package_mentions_from_text(table.title)
        for label in title_labels:
            registry.register(
                label,
                role=_package_label_role(label),
                evidence=PackageEvidence("table_title", table.table_id, table.title, 0.98),
            )
        for heading in table.current_chapter_titles:
            heading_labels = _package_mentions_from_text(heading)
            for label in heading_labels:
                registry.register(
                    label,
                    role=_package_label_role(label),
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
        "selected_label": _clean_output_package_label(assignment.selected_label),
        "mode": assignment.mode,
        "confidence": assignment.confidence,
        "reason": assignment.reason,
    }


def _resolve_direct_assignment(
    target: PackageTargetTable,
    registry: PackageRegistry,
    plan: MultiPackagePlanLike | None,
) -> TablePackageAssignment:
    """按“多封装 > 表题 > 表头 > 当前章节”处理局部证据。"""

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

    # 表题和表头都只接受具体 package_identity。QFN、BGA 等 family 即使
    # 出现在标题中，也不能压过表头中的 SF2507、ZCE 等具体身份。
    title_keys = registry.specific_keys_in_text(target.title)
    header_text = "\n".join(target.headers)
    header_keys = registry.specific_keys_in_text(header_text)
    title_family_candidates = registry.specific_keys(
        registry.all_keys_in_text(target.title)
    )
    header_family_candidates = registry.specific_keys(
        registry.all_keys_in_text(header_text)
    )
    header_keys.update(
        registry.specific_keys(
            registry.keys_for_table_sources(
                target.table_id,
                {
                    "package_drawing_column",
                    "package_name_column",
                    "package_type_column",
                },
            )
        )
    )

    if len(title_keys) > 1:
        return TablePackageAssignment(
            (),
            "conflict",
            0.0,
            "当前表题同时出现多个封装（均为具体身份），但未检测到多封装绑定",
        )
    if not title_keys and len(title_family_candidates) > 1:
        return TablePackageAssignment(
            (),
            "conflict",
            0.0,
            "当前表题只有封装家族信息，该家族对应多个封装，保持未解析",
        )
    if len(header_keys) > 1:
        return TablePackageAssignment(
            (),
            "conflict",
            0.0,
            "当前表头同时出现多个封装（均为具体身份），但未检测到多封装绑定",
        )
    if not header_keys and len(header_family_candidates) > 1:
        return TablePackageAssignment(
            (),
            "conflict",
            0.0,
            "当前表头只有封装家族信息，该家族对应多个封装，保持未解析",
        )
    if title_keys and header_keys and title_keys != header_keys:
        return TablePackageAssignment(
            (),
            "conflict",
            0.0,
            "当前表题与表头指向不同具体封装，保持未解析",
        )
    if len(title_keys) == 1:
        package_key = next(iter(title_keys))
        return TablePackageAssignment(
            (package_key,),
            "table_title",
            0.98,
            "当前表格标题明确包含唯一封装",
            selected_label=registry.best_label_for_key_in_text(
                package_key,
                target.title,
            ),
        )
    if len(header_keys) == 1:
        package_key = next(iter(header_keys))
        return TablePackageAssignment(
            (package_key,),
            "table_header",
            0.95,
            "当前表头明确包含唯一具体封装",
            selected_label=registry.best_label_for_key_in_text(
                package_key,
                header_text,
            ),
        )

    # 兼容规则层已经从当前表题得到的 pkg，但它不能覆盖表题/表头冲突。
    if target.declared_package:
        declared_keys = registry.specific_keys(
            registry.keys_for_label(target.declared_package)
        )
        if len(declared_keys) == 1:
            package_key = next(iter(declared_keys))
            return TablePackageAssignment(
                (package_key,),
                "rule_declared",
                0.94,
                "规则层从当前表局部信息得到唯一封装",
                selected_label=(
                    _clean_output_package_label(target.declared_package)
                    if registry.unique_key_for_label(target.declared_package)
                    == package_key
                    else ""
                ),
            )

    heading_keys: set[str] = set()
    for heading in target.current_chapter_titles:
        heading_keys.update(registry.specific_keys_in_text(heading))
    if len(heading_keys) == 1:
        package_key = next(iter(heading_keys))
        heading_text = "\n".join(target.current_chapter_titles)
        return TablePackageAssignment(
            (package_key,),
            "current_chapter",
            0.9,
            "当前章节上下文只包含一个明确封装",
            selected_label=registry.best_label_for_key_in_text(
                package_key,
                heading_text,
            ),
        )
    if len(heading_keys) > 1:
        return TablePackageAssignment(
            (),
            "current_chapter_conflict",
            0.0,
            "当前章节同时包含多个封装，不能直接继承",
        )

    return TablePackageAssignment(reason="未发现表格局部封装证据")


def _resolve_semantic_assignments(
    targets: Sequence[PackageTargetTable],
    assignments: dict[int, TablePackageAssignment],
    registry: PackageRegistry,
    blocks: Sequence[PackageDocumentBlock],
    *,
    classifier: PackageAssignmentClassifier | None,
) -> list[dict[str, Any]]:
    """让模型为未归属表选择已有 package_id，并校验返回证据。"""

    unresolved = [
        target
        for target in targets
        if assignments[target.table_id].mode == "unresolved"
    ]
    candidates = [
        candidate
        for candidate in registry.candidates.values()
        if candidate.role == "package_identity"
    ]
    if not unresolved or not candidates:
        return []

    classify = classifier or _classify_table_package_assignment
    workers = max(
        1,
        int(
            os.getenv(
                "EXTRACT_PACKAGE_WORKERS",
                os.getenv("EXTRACT_SCHEMA_WORKERS", "4"),
            )
        ),
    )
    block_map = {block.block_id: block for block in blocks}
    diagnostics: list[dict[str, Any]] = []
    print(
        f"表格封装归属: 待判断 {len(unresolved)} 张, 候选封装 {len(candidates)} 个, "
        f"并发 {min(workers, len(unresolved))}"
    )
    with ThreadPoolExecutor(max_workers=min(workers, len(unresolved))) as executor:
        futures = {}
        for target in unresolved:
            context_blocks = _context_blocks_for_table(target.table_id, blocks)
            futures[executor.submit(classify, target, context_blocks, candidates)] = (
                target,
                context_blocks,
            )
        completed = 0
        for future in as_completed(futures):
            target, context_blocks = futures[future]
            try:
                response = future.result()
            except Exception as exc:
                diagnostics.append(
                    {
                        "stage": "table_package_assignment",
                        "table_id": target.table_id,
                        "status": "error",
                        "reason": str(exc),
                    }
                )
                completed += 1
                print(f"表格封装归属进度: {completed}/{len(unresolved)}")
                continue

            assignment, reason = _validate_semantic_assignment(
                response,
                target=target,
                registry=registry,
                allowed_blocks={block.block_id for block in context_blocks},
                all_blocks=block_map,
            )
            if assignment is not None:
                assignments[target.table_id] = assignment
            diagnostics.append(
                {
                    "stage": "table_package_assignment",
                    "table_id": target.table_id,
                    "status": "accepted" if assignment is not None else "rejected",
                    "reason": reason,
                    "response": response,
                }
            )
            completed += 1
            print(f"表格封装归属进度: {completed}/{len(unresolved)}")
    return diagnostics


def _classify_table_package_assignment(
    target: PackageTargetTable,
    context_blocks: Sequence[PackageDocumentBlock],
    candidates: Sequence[PackageCandidate],
) -> Mapping[str, Any]:
    """调用模型判断一张表与已验证封装实体的语义关系。"""

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("封装语义判断需要环境变量 DEEPSEEK_API_KEY")
    payload = {
        "task": (
            "Assign this complete physical-pin table to one existing package_id, "
            "or leave it unresolved."
        ),
        "rules": [
            "Select only from candidate package_id values supplied below.",
            "Do not create, rename, merge, or translate packages.",
            "Use the full table, title, headers, heading path, and nearby ordered blocks.",
            "A package mentioned in the current table title is stronger than one only in nearby text.",
            "A package mentioned in the current table headers is next strongest.",
            "Return no package_id when evidence conflicts or is insufficient.",
            "This task is single-package assignment; never return multiple package IDs.",
            "Cite only supplied evidence block_id values.",
        ],
        "target_table": {
            "table_id": target.table_id,
            "title": target.title,
            "heading_path": list(target.current_chapter_titles),
            "headers": list(target.headers),
            "rows": [list(row) for row in target.data_rows],
        },
        "nearby_blocks": [_block_to_payload(block) for block in context_blocks],
        "package_candidates": [
            {
                "package_id": candidate.key,
                "names": candidate.aliases,
                "drawing_code": candidate.drawing_code,
                "family": candidate.family,
                "pin_count": candidate.pin_count,
                "device_scope": candidate.device_scope,
            }
            for candidate in candidates
        ],
        "output_schema": {
            "table_id": target.table_id,
            "package_id": "one supplied package_id or empty string",
            "evidence_block_ids": ["block-id"],
        },
    }
    return call_model_json(
        payload,
        api_key=api_key,
        system_prompt=(
            "You assign one complete semiconductor pin table to an existing validated "
            "package_id. Return JSON containing only table_id, package_id and "
            "evidence_block_ids. Never create a package name."
        ),
        max_tokens=int(os.getenv("EXTRACT_PACKAGE_ASSIGN_MAX_TOKENS", "3000")),
        timeout=float(os.getenv("EXTRACT_PACKAGE_TIMEOUT", "60")),
    )


def _validate_semantic_assignment(
    response: Any,
    *,
    target: PackageTargetTable,
    registry: PackageRegistry,
    allowed_blocks: set[str],
    all_blocks: Mapping[str, PackageDocumentBlock],
) -> tuple[TablePackageAssignment | None, str]:
    """拒绝未知 package_id、错误 table_id 和无效证据。"""

    if not isinstance(response, Mapping):
        return None, "模型返回不是对象"
    try:
        response_table_id = int(response.get("table_id"))
    except (TypeError, ValueError):
        return None, "table_id 无效"
    if response_table_id != target.table_id:
        return None, "table_id 与当前表不一致"
    package_key = str(response.get("package_id") or "").strip()
    if not package_key:
        return None, "模型认为证据不足"
    candidate = registry.candidates.get(package_key)
    if candidate is None or candidate.role != "package_identity":
        return None, "返回了未知或非具体 package_id"
    evidence_ids = _unique_strings(response.get("evidence_block_ids"))
    if not evidence_ids:
        return None, "缺少证据块"
    if any(
        block_id not in allowed_blocks or block_id not in all_blocks
        for block_id in evidence_ids
    ):
        return None, "证据块不在当前表上下文中"
    return (
        TablePackageAssignment(
            (package_key,),
            "semantic_context",
            0.86,
            f"语义上下文证据：{', '.join(evidence_ids)}",
            selected_label=registry.best_label_for_key_in_text(
                package_key,
                "\n".join(all_blocks[block_id].text for block_id in evidence_ids),
            ),
        ),
        "",
    )


def _resolve_continuation_assignments(
    targets: Sequence[PackageTargetTable],
    assignments: dict[int, TablePackageAssignment],
) -> None:
    """规范化表题相同的续表继承唯一 pkg。"""

    title_packages: dict[str, list[TablePackageAssignment]] = {}
    for target in targets:
        assignment = assignments[target.table_id]
        keys = assignment.package_keys
        title_key = _normalized_continuation_title(target.title)
        if title_key and len(keys) == 1:
            title_packages.setdefault(title_key, []).append(assignment)

    for target in targets:
        if assignments[target.table_id].mode != "unresolved":
            continue
        title_key = _normalized_continuation_title(target.title)
        source_assignments = title_packages.get(title_key, [])
        keys = {
            assignment.package_keys[0]
            for assignment in source_assignments
            if len(assignment.package_keys) == 1
        }
        if title_key and len(keys) == 1:
            selected_labels = {
                _clean_output_package_label(assignment.selected_label)
                for assignment in source_assignments
                if _clean_output_package_label(assignment.selected_label)
            }
            assignments[target.table_id] = TablePackageAssignment(
                tuple(keys),
                "continuation",
                0.9,
                "规范化后的 Table 编号/标题与已归属续表完全一致",
                selected_label=(
                    next(iter(selected_labels)) if len(selected_labels) == 1 else ""
                ),
            )


def _resolve_unique_section_assignments(
    targets: Sequence[PackageTargetTable],
    assignments: dict[int, TablePackageAssignment],
) -> None:
    """同一最深章节只有一个明确封装时，为未标注表继承该封装。"""

    scope_packages: dict[str, list[TablePackageAssignment]] = {}
    for target in targets:
        assignment = assignments[target.table_id]
        if len(assignment.package_keys) != 1:
            continue
        scope = _section_scope(target.current_chapter_titles)
        if scope:
            scope_packages.setdefault(scope, []).append(assignment)

    for target in targets:
        if assignments[target.table_id].mode != "unresolved":
            continue
        scope = _section_scope(target.current_chapter_titles)
        source_assignments = scope_packages.get(scope, [])
        keys = {
            assignment.package_keys[0]
            for assignment in source_assignments
            if len(assignment.package_keys) == 1
        }
        if scope and len(keys) == 1:
            selected_labels = {
                _clean_output_package_label(assignment.selected_label)
                for assignment in source_assignments
                if _clean_output_package_label(assignment.selected_label)
            }
            assignments[target.table_id] = TablePackageAssignment(
                tuple(keys),
                "same_section",
                0.82,
                "同一最深章节中的明确目标表只属于一个封装",
                selected_label=(
                    next(iter(selected_labels)) if len(selected_labels) == 1 else ""
                ),
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
            raw_identity_labels = _unique_labels([
                *values_by_role.get("drawing", []),
                *values_by_role.get("name", []),
                *values_by_role.get("package", []),
            ])
            identity_labels = [
                label
                for label in raw_identity_labels
                if _package_label_role(label) == "package_identity"
            ]
            family_labels = _unique_labels(
                [
                    *values_by_role.get("type", []),
                    *(
                        label
                        for label in raw_identity_labels
                        if _package_label_role(label) == "package_family"
                    ),
                ]
            )
            labels = _unique_labels(identity_labels or family_labels)
            if not labels:
                continue
            pin_count = _extract_pin_count(
                _cell(row, pin_count_index) if pin_count_index is not None else ""
            )
            drawing = next(iter(values_by_role.get("drawing", [])), "")
            family = next(iter(family_labels), "")
            source = (
                "package_drawing_column"
                if drawing
                else "package_name_column"
                if values_by_role.get("name") or values_by_role.get("package")
                else "package_type_column"
            )
            identity_role = "package_identity" if identity_labels else "package_family"
            registry.register(
                labels[0],
                aliases=labels[1:],
                drawing_code=drawing,
                family=family,
                pin_count=pin_count,
                role=identity_role,
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


def _package_label_role(value: str) -> str:
    """区分只能作上下文的封装家族与可以输出的具体封装身份。"""

    normalized = _normalize(value)
    generic_families = {
        "bga",
        "hsbga",
        "nfbga",
        "fbga",
        "pbga",
        "vfbga",
        "qfn",
        "vqfn",
        "wqfn",
        "lqfp",
        "tqfp",
        "qfp",
        "sop",
        "ssop",
        "tssop",
        "soic",
        "dip",
        "mlf",
        "dfn",
        "csp",
        "wcsp",
    }
    tokens = set(normalized.split())
    if normalized in generic_families:
        return "package_family"
    # “128 pin LQFP”仍然只是机械家族和脚数，不是控制引脚映射的身份。
    family_descriptors = {
        "u",
        "micro",
        "microstar",
        "jr",
        "mini",
        "fine",
        "pitch",
        "exposed",
        "pad",
        "powerpad",
        "pin",
        "ball",
    }
    non_numeric = {token for token in tokens if not token.isdigit()}
    if non_numeric and non_numeric <= generic_families:
        return "package_family"
    if tokens & generic_families and non_numeric <= generic_families | family_descriptors:
        return "package_family"
    return "package_identity"


def _chunk_document_blocks(
    blocks: Sequence[PackageDocumentBlock],
) -> list[tuple[PackageDocumentBlock, ...]]:
    """按完整块边界分批，保证全文所有块都且只发送一次。"""

    max_chars = max(4000, int(os.getenv("EXTRACT_PACKAGE_CHUNK_CHARS", "12000")))
    chunks: list[tuple[PackageDocumentBlock, ...]] = []
    current: list[PackageDocumentBlock] = []
    current_chars = 0
    for block in blocks:
        block_chars = len(block.text)
        if current and current_chars + block_chars > max_chars:
            chunks.append(tuple(current))
            current = []
            current_chars = 0
        current.append(block)
        current_chars += block_chars
        # 超长表格独占一个请求，但不能截成 sample_rows 或丢失后半段。
        if block_chars >= max_chars:
            chunks.append(tuple(current))
            current = []
            current_chars = 0
    if current:
        chunks.append(tuple(current))
    return chunks


def _context_blocks_for_table(
    table_id: int,
    blocks: Sequence[PackageDocumentBlock],
) -> tuple[PackageDocumentBlock, ...]:
    """返回目标表及其前后邻近块；目标表完整数据另在 payload 中提供。"""

    position = next(
        (
            index
            for index, block in enumerate(blocks)
            if block.block_type == "table" and block.table_id == table_id
        ),
        None,
    )
    if position is None:
        return tuple()
    radius = max(2, int(os.getenv("EXTRACT_PACKAGE_CONTEXT_BLOCKS", "8")))
    return tuple(blocks[max(0, position - radius) : position + radius + 1])


def _block_to_payload(block: PackageDocumentBlock) -> dict[str, Any]:
    return {
        "block_id": block.block_id,
        "order": block.order,
        "type": block.block_type,
        "table_id": block.table_id,
        "heading_path": list(block.heading_path),
        "text": block.text,
    }


def _unique_strings(value: Any) -> list[str]:
    """把模型数组字段规范成去重字符串列表。"""

    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _text_contains_exact_label(text: str, label: str) -> bool:
    """按完整字母数字边界验证模型返回名称确实来自证据原文。"""

    normalized_text = _normalize(text)
    normalized_label = _normalize(label)
    if not normalized_label:
        return False
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_label)}(?![a-z0-9])",
            normalized_text,
        )
    )


def _normalized_continuation_title(value: str) -> str:
    text = _clean_text(value)
    text = re.sub(
        r"[\s（(]*(?:continued|续表|续)[\s）)]*",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return _normalize(text)


def _clean_markdown_text(value: str) -> str:
    text = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", str(value or ""))
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.MULTILINE)
    return _clean_text(text)


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


def _candidate_key(
    primary: str,
    drawing_code: str,
    family: str,
    pin_count: str,
    device_scope: str,
) -> str:
    if drawing_code:
        base = f"drawing={_normalize(drawing_code)}"
    else:
        base = f"label={_normalize(primary or family) or 'unknown'}"
    scope = f"|scope={_normalize(device_scope)}" if device_scope else ""
    pins = f"|pins={pin_count}" if pin_count else ""
    return f"{base}{scope}{pins}"


def _candidate_is_compatible(
    candidate: PackageCandidate,
    *,
    drawing_code: str,
    pin_count: str,
    device_scope: str,
) -> bool:
    if candidate.drawing_code and drawing_code:
        if _normalize(candidate.drawing_code) != _normalize(drawing_code):
            return False
    if candidate.pin_count and pin_count and candidate.pin_count != pin_count:
        return False
    if candidate.device_scope and device_scope:
        if _normalize(candidate.device_scope) != _normalize(device_scope):
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


def _clean_output_package_label(value: str) -> str:
    """清理最终 pkg；发现别名拼接符时拒绝整串，不能截取其中一段猜测。"""

    cleaned = _clean_package_label(value)
    if "|" in cleaned:
        return ""
    return cleaned


def _is_specific_output_package_label(value: str) -> bool:
    """判断名称能否作为一个独立 pkg 输出，机械家族只保留为内部上下文。"""

    cleaned = _clean_output_package_label(value)
    return bool(
        cleaned
        and _package_label_role(cleaned) == "package_identity"
        and _looks_like_package_value(cleaned)
    )


def _package_output_label_priority(
    value: str,
    *,
    source: str = "",
    drawing_code: str = "",
    is_primary: bool = False,
) -> tuple[int, ...]:
    """为同一实体的别名排序，不通过拼接制造新的 pkg 名称。

    长度不超过 15 只是优先信号，不是硬截断条件。证据充分但更长的具体名称
    仍可以作为最后兜底，避免代码擅自改写原文。
    """

    cleaned = _clean_output_package_label(value)
    if not _is_specific_output_package_label(cleaned):
        return (0,)
    source_priority = {
        "multi_package_plan": 100,
        "table_title": 95,
        "package_drawing_column": 92,
        "package_name_column": 88,
        "package_type_column": 75,
        "semantic_document": 70,
        "current_heading": 60,
    }.get(source, 50)
    normalized = _normalize(cleaned)
    normalized_drawing = _normalize(drawing_code)
    return (
        1,
        int(bool(normalized_drawing and normalized == normalized_drawing)),
        source_priority,
        int(len(cleaned) <= 15),
        int(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", cleaned))),
        int(is_primary),
        -len(cleaned),
    )


def _select_candidate_canonical_name(candidate: PackageCandidate) -> str:
    """从候选实体内部别名中选择一个规范名称，绝不返回别名列表。"""

    eligible = [
        alias
        for alias in candidate.aliases
        if _is_specific_output_package_label(alias)
    ]
    if not eligible:
        return ""
    return max(
        eligible,
        key=lambda alias: candidate.alias_priorities.get(
            alias,
            _package_output_label_priority(
                alias,
                drawing_code=candidate.drawing_code,
            ),
        ),
    )


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
