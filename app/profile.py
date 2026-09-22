"""文档画像（profile）：用已保存的新版 schema 批量校验真实文档。

输入最多 ``MAX_PROFILE_DOCUMENTS`` 个 JSON 对象，对每个文档调用
``jsonschema``，输出：

* **合规率**：通过 / 待检文档数（精确分数 + 保留 6 位小数的比率）；
* **失败归并**：按 *实例 JSON Pointer + 规则指纹* 把同类失败合并，
  给出失败文档数与示例下标。

---------------------------------------------------------------------------
oneOf / anyOf 嵌套错误：最深公共失败路径
---------------------------------------------------------------------------
``jsonschema`` 对组合关键字的失败会生成一棵错误树：顶层是 oneOf/anyOf
节点，``error.context`` 内是各分支的错误（分支内可能继续嵌套组合
关键字）。直接使用顶层节点会把结构完全不同的失败都归到组合所在路径，
丢失定位；直接使用叶子则无法合并"同一组合不同分支"的同类问题。

因此对每棵错误树：

1. 收集全部**叶子错误**（``context`` 为空者）；``false`` 分支产生的
   叶子 ``validator`` 为 ``None``，统一记为 ``false_schema``。
2. **若所有叶子同路径、同 validator**（如
   ``anyOf:[{type:string},{type:integer}]`` 对数组取值的失败，两个
   分支都报 ``#/a`` 上的 ``type``），失败定位就是该路径，规则按
   validator 归并，指纹主体取各分支 ``validator_value`` 的规范并集。
3. **否则**取所有叶子实例路径的**最长公共前缀**作为最深公共失败
   路径，并选择在 schema 路径上覆盖全部叶子的**最顶层组合节点**
   （其 schema path 是每条叶子 schema path 的前缀）作为归并规则：
   例如文档同时不满足 ``#/a`` 与 ``#/b`` 两个分支时，路径归并到
   ``#``，规则归并到拥有这两个分支的 ``oneOf``。

---------------------------------------------------------------------------
稳定规则指纹
---------------------------------------------------------------------------
规则指纹是 ``SHA-256("<instance pointer>\\x1f<validator>\\x1f<subject>")``
取前 16 个十六进制字符，**与输入顺序、实例取值均无关**：

* ``type`` / ``enum`` / ``const`` / ``minimum`` … 等数据无关规则：
  ``subject`` 是 ``validator_value`` 的规范 JSON（排序键、紧凑分隔）；
* ``required``：subject 是该叶子实际缺失的单个属性名
  （``required`` 每个缺失属性是一条独立叶子）；
* ``additionalProperties: false``：subject 为 ``additionalProperties``，
  不同多余字段名同属一类；
* oneOf/anyOf 多分支命中：subject 为 ``multiple_matches``；
* 第 2 种同路径同 validator 的组合归并：subject 为各分支规则值的
  规范并集；
* 第 3 种公共路径组合归并：subject 为 ``branch_mismatch``。

因此打乱待检文档顺序只会改变示例的收集过程，不会改变分组集合与
分组顺序（分组最终按 (path, validator, fingerprint) 稳定排序）。
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any

# 单次画像允许的最大文档数
MAX_PROFILE_DOCUMENTS = 5000

# 每组保留的示例下标上限（取最小的若干个，与输入顺序无关）
MAX_EXAMPLE_INDICES = 10

_FINGERPRINT_LEN = 16
_FIELD_SEP = "\x1f"

# 组合关键字（这些节点的错误带 context，是"归并节点"而非具体规则）
_COMPOSITION_VALIDATORS = ("anyOf", "oneOf", "allOf", "not")
_FALSE_SCHEMA = "false_schema"
_MULTIPLE_MATCHES = "multiple_matches"
_BRANCH_MISMATCH = "branch_mismatch"
# additionalProperties: false 的指纹主体：多余字段名是实例数据，不参与指纹
_ADDITIONAL_PROPERTIES = "additionalProperties"


# ---------------------------------------------------------------------------
# 路径与指纹
# ---------------------------------------------------------------------------

def _escape_segment(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def instance_pointer(path: list) -> str:
    """实例 deque 路径 -> 根为 ``#`` 的 JSON Pointer。"""
    if not path:
        return "#"
    return "#/" + "/".join(_escape_segment(str(p)) for p in path)


def canonical(value: Any) -> str:
    """规范 JSON 序列化：排序键、无空白，使指纹对 dict 顺序不敏感。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def rule_fingerprint(pointer: str, validator: str, subject: str) -> str:
    """生成与输入顺序 / 实例取值无关的稳定规则指纹。"""
    key = _FIELD_SEP.join([pointer, validator, subject])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]


def _common_prefix(paths: list[list]) -> list:
    """若干实例路径的最长公共前缀（逐段比较）。"""
    if not paths:
        return []
    prefix = list(paths[0])
    for path in paths[1:]:
        bound = min(len(prefix), len(path))
        i = 0
        while i < bound and prefix[i] == path[i]:
            i += 1
        prefix = prefix[:i]
        if not prefix:
            break
    return prefix


# ---------------------------------------------------------------------------
# 叶子错误收集
# ---------------------------------------------------------------------------

def _leaf_errors(error) -> list:
    """递归收集错误树的全部叶子（context 为空者），去重后返回。

    同一组合分支内 jsonschema 偶尔会挂出结构相同的重复错误，这里按
    (实例路径, schema 路径, validator, 消息) 去重，避免重复计数。
    """
    leaves: list = []
    seen: set[tuple] = set()

    def walk(err) -> None:
        if err.context:
            for child in err.context:
                walk(child)
            return
        key = (
            tuple(str(p) for p in err.absolute_path),
            tuple(str(p) for p in err.absolute_schema_path),
            err.validator or _FALSE_SCHEMA,
            err.message,
        )
        if key not in seen:
            seen.add(key)
            leaves.append(err)

    walk(error)
    return leaves


def _validator_name(error) -> str:
    return error.validator or _FALSE_SCHEMA


# ---------------------------------------------------------------------------
# 单条规则失败：指纹主体（与实例取值解耦）
# ---------------------------------------------------------------------------

def _leaf_subject(error) -> str:
    """提取叶子规则的指纹主体。"""
    validator = _validator_name(error)
    if validator == "required":
        # required 每个缺失属性是一条独立叶子，消息为 "'name' is a required property"；
        # 直接用 schema 声明值与实例键的差集在该叶子路径上反推出缺失属性。
        node = error.instance
        declared = error.validator_value or []
        if isinstance(node, dict):
            missing = [name for name in declared if name not in node]
            if len(missing) == 1:
                return canonical(missing[0])
        # 兜底：从消息中解析引号内的属性名
        message = error.message
        if message.endswith("is a required property"):
            return canonical(message.split(" is a required property", 1)[0].strip("'\""))
        return canonical(declared)
    if validator == "additionalProperties":
        # additionalProperties: false / 或 patternProperties 不匹配：
        # 具体多余字段名是实例数据，不计入指纹，统一归为一类。
        return _ADDITIONAL_PROPERTIES
    return canonical(error.validator_value)


def _is_multiple_match(error) -> bool:
    return error.validator in ("oneOf", "anyOf") and not error.context


# ---------------------------------------------------------------------------
# 把一棵错误树归并为若干 (path, validator, subject, message) 失败规则
# ---------------------------------------------------------------------------

def _select_owner_composition(leaves: list):
    """选择在 schema 路径上覆盖全部叶子的最顶层组合节点。

    所有叶子的 schema path 都源于同一棵组合错误树。取全部叶子 schema
    path 的公共前缀，其中**自顶向下第一个组合关键字层**（token 为
    oneOf/anyOf/allOf/not）就是同时覆盖所有分歧分支的最顶层组合：
    内层组合的 token 位于更深的公共前缀之外，自然不会被选中。
    """
    if not leaves:
        return None
    # 每条叶子的 schema path 元组
    leaf_paths = [tuple(str(p) for p in leaf.absolute_schema_path) for leaf in leaves]
    # 公共 schema 前缀
    common = list(leaf_paths[0])
    for path in leaf_paths[1:]:
        bound = min(len(common), len(path))
        i = 0
        while i < bound and common[i] == path[i]:
            i += 1
        common = common[:i]
    # 自顶向下找公共前缀中第一个组合关键字层
    for token in common:
        if token in _COMPOSITION_VALIDATORS:
            return token
    # 公共前缀没有组合层（理论上不发生，因为错误树根就是组合节点）：
    # 退化为第一条叶子的顶层组合。
    for token in leaf_paths[0]:
        if token in _COMPOSITION_VALIDATORS:
            return token
    return leaves[0].validator


def reduce_error_tree(error) -> list[tuple[list, str, str, str]]:
    """把一条顶层 ValidationError 归并为若干失败规则。

    返回 ``[(instance_path_tokens, validator, subject, message), ...]``
    （多数顶层错误只产生 1 条；顶层非组合错误本身即叶子）。
    """
    if not error.context:
        # 顶层就是具体规则（required / type / 多命中 oneOf …）
        validator = _validator_name(error)
        if _is_multiple_match(error):
            return [(list(error.absolute_path), validator, _MULTIPLE_MATCHES, error.message)]
        return [(list(error.absolute_path), validator, _leaf_subject(error), error.message)]

    leaves = _leaf_errors(error)
    if not leaves:
        return []
    if len(leaves) == 1:
        leaf = leaves[0]
        validator = _validator_name(leaf)
        return [(list(leaf.absolute_path), validator, _leaf_subject(leaf), leaf.message)]

    leaf_paths = [list(leaf.absolute_path) for leaf in leaves]
    same_path = all(path == leaf_paths[0] for path in leaf_paths)
    validators = {_validator_name(leaf) for leaf in leaves}

    if same_path and len(validators) == 1:
        # 同一实例位置、同一规则（如联合类型不同期望值）：规则值取并集，
        # 仍归并到该位置上的具体 validator。
        leaf = leaves[0]
        validator = _validator_name(leaf)
        values: list = []
        for leaf_error in leaves:
            value = leaf_error.validator_value
            if value not in values:
                values.append(value)
        # 各分支规则值先各自规范化为字符串再排序取并集，规避不可比较类型
        subject = canonical(sorted(canonical(v) for v in values))
        # 代表消息取叶子消息里字典序最小者（与输入顺序无关）
        message = min(leaf.message for leaf in leaves)
        return [(leaf_paths[0], validator, subject, message)]

    # 分歧：归并到最深公共实例路径 + 最顶层覆盖组合节点
    common = _common_prefix(leaf_paths)
    owner = _select_owner_composition(leaves) or _validator_name(error)
    message = min(leaf.message for leaf in leaves)
    return [(common, owner, _BRANCH_MISMATCH, message)]


# ---------------------------------------------------------------------------
# 画像
# ---------------------------------------------------------------------------

def profile_documents(schema: Any, documents: list[dict]) -> dict:
    """对文档列表用 schema 批量校验并归并失败。

    纯函数：不读写数据库、不修改入参。``schema`` 必须已经过
    ``app.compat.make_validator`` 校验合法。
    """
    from app.compat import make_validator

    validator = make_validator(schema)

    total = len(documents)
    valid_count = 0
    # (pointer, validator, fingerprint) -> 归并组
    groups: "OrderedDict[tuple, dict]" = OrderedDict()

    for index, document in enumerate(documents):
        errors = list(validator.iter_errors(document))
        if not errors:
            valid_count += 1
            continue

        # 同一文档内同指纹只计一次（多个字段 / 分支触发同一规则时不重复计数）
        seen_in_doc: set[tuple] = set()
        for error in errors:
            for path_tokens, rule, subject, message in reduce_error_tree(error):
                pointer = instance_pointer(path_tokens)
                fingerprint = rule_fingerprint(pointer, rule, subject)
                key = (pointer, rule, fingerprint)
                if key in seen_in_doc:
                    continue
                seen_in_doc.add(key)

                group = groups.get(key)
                if group is None:
                    groups[key] = {
                        "path": pointer,
                        "validator": rule,
                        "fingerprint": fingerprint,
                        "document_count": 1,
                        "example_indices": [index],
                        # 代表消息取字典序最小，保证与输入顺序无关
                        "message": message,
                    }
                else:
                    group["document_count"] += 1
                    if len(group["example_indices"]) < MAX_EXAMPLE_INDICES:
                        group["example_indices"].append(index)
                    if message < group["message"]:
                        group["message"] = message

    invalid_count = total - valid_count
    rate = round(valid_count / total, 6) if total else 0.0

    failures = sorted(groups.values(), key=lambda g: (g["path"], g["validator"], g["fingerprint"]))
    # 示例下标按升序（收集到的未必有序，因为先到先得但顺序遍历本身递增；
    # 显式排序以保证稳定）。
    for group in failures:
        group["example_indices"] = sorted(group["example_indices"])

    return {
        "total": total,
        "valid": valid_count,
        "invalid": invalid_count,
        "compliance_rate": rate,
        "failures": failures,
    }
