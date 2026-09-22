"""文档画像引擎（app.profile）单元测试。

重点：
* 合规率与计数；
* oneOf/anyOf 嵌套错误 -> 最深公共失败路径 + 稳定规则指纹；
* 同路径同 validator 的组合失败归并到具体规则；分歧归并到组合节点；
* 分组与输入顺序无关；
* 指纹对实例取值不敏感。
"""
from app import profile
from app.profile import (
    MAX_PROFILE_DOCUMENTS,
    canonical,
    instance_pointer,
    profile_documents,
    reduce_error_tree,
    rule_fingerprint,
)
from app.compat import make_validator


def _rules(schema, doc):
    """返回 (pointer, validator, subject) 三元组列表，便于断言归并结果。"""
    validator = make_validator(schema)
    out = []
    for error in validator.iter_errors(doc):
        for path_tokens, rule, subject, _message in reduce_error_tree(error):
            out.append((instance_pointer(path_tokens), rule, subject))
    return out


class TestPointerAndFingerprint:
    def test_instance_pointer_root_and_path(self):
        assert instance_pointer([]) == "#"
        assert instance_pointer(["a", 0, "b"]) == "#/a/0/b"

    def test_instance_pointer_escapes_slash_and_tilde(self):
        assert instance_pointer(["a/b", "c~d"]) == "#/a~1b/c~0d"

    def test_fingerprint_stable_and_order_independent(self):
        fp1 = rule_fingerprint("#/a", "type", '"string"')
        fp2 = rule_fingerprint("#/a", "type", '"string"')
        assert fp1 == fp2 and len(fp1) == 16
        # subject 不同 -> 指纹不同
        assert rule_fingerprint("#/a", "type", '"string"') != \
            rule_fingerprint("#/a", "type", '"integer"')
        # path 不同 -> 指纹不同
        assert rule_fingerprint("#/a", "type", '"string"') != \
            rule_fingerprint("#/b", "type", '"string"')

    def test_canonical_ignores_key_order(self):
        assert canonical({"b": 1, "a": 2}) == canonical({"a": 2, "b": 1})


class TestCompositionReduction:
    def test_anyof_same_path_same_validator_merges_to_rule(self):
        # 数组取值既不是 string 也不是 integer：两个叶子同路径同 validator，
        # 归并到 #/a 上的 type（而非 anyOf 节点）。
        schema = {"properties": {"a": {"anyOf": [{"type": "string"},
                                                 {"type": "integer"}]}}}
        rules = _rules(schema, {"a": [1, 2]})
        assert ("#/a", "type", canonical(['"integer"', '"string"'])) in rules
        assert all(rule != "anyOf" for _, rule, _ in rules)

    def test_oneof_divergent_branches_collapse_to_common_path(self):
        # 两分支分别要求 #/a 为 string、#/b 为 number，文档两边都不满足，
        # 最深公共路径为 #，归并到拥有分支的 oneOf。
        schema = {"oneOf": [
            {"type": "object", "required": ["a"],
             "properties": {"a": {"type": "string"}}},
            {"type": "object", "required": ["b"],
             "properties": {"b": {"type": "number"}}},
        ]}
        rules = _rules(schema, {"a": 1, "b": "x"})
        assert rules == [("#", "oneOf", "branch_mismatch")]

    def test_nested_oneof_common_path_inside_property(self):
        # 两分支都在 #/kind 下分别 required x / y，文档 {} 两边都缺：
        # 叶子同路径（#/kind）同 validator（required），最深归并到
        # #/kind 的 required，主体为两分支 required 列表的规范并集。
        schema = {"properties": {"kind": {"oneOf": [
            {"type": "object", "required": ["x"],
             "properties": {"x": {"type": "string"}}},
            {"type": "object", "required": ["y"],
             "properties": {"y": {"type": "number"}}},
        ]}}}
        rules = _rules(schema, {"kind": {}})
        assert rules == [
            ("#/kind", "required", canonical(sorted([canonical(["x"]), canonical(["y"])])))
        ]

    def test_nested_oneof_divergent_rule_collapses_to_composition(self):
        # 一边 x 类型错误（#/kind/x type）、一边缺 y（#/kind required），
        # 路径 / 规则都分歧 -> 归并到 #/kind 的 oneOf。
        schema = {"properties": {"kind": {"oneOf": [
            {"type": "object", "required": ["x"],
             "properties": {"x": {"type": "string"}}},
            {"type": "object", "required": ["y"],
             "properties": {"y": {"type": "number"}}},
        ]}}}
        rules = _rules(schema, {"kind": {"x": 1}})
        assert rules == [("#/kind", "oneOf", "branch_mismatch")]

    def test_nested_composition_picks_outermost_covering_composition(self):
        # 外层 anyOf 含一个内部分叉的 oneOf 分支与一个 integer 分支：
        # 叶子跨内层 oneOf 与外层 integer 分支，最顶层覆盖组合是 anyOf。
        schema = {"anyOf": [
            {"oneOf": [{"required": ["x"]}, {"required": ["y"]}]},
            {"type": "integer"},
        ]}
        rules = _rules(schema, {})
        assert ("#", "anyOf", "branch_mismatch") in rules

    def test_oneof_multiple_match_is_composition_rule(self):
        schema = {"oneOf": [{"type": "object"}, {"required": ["a"]}]}
        rules = _rules(schema, {"a": 1})
        assert rules == [("#", "oneOf", "multiple_matches")]

    def test_false_schema_branch(self):
        schema = {"anyOf": [False, {"type": "string"}]}
        rules = _rules(schema, 1)
        # 同路径，两个叶子 validator 不同（false_schema / type）-> 分歧归并
        assert ("#", "anyOf", "branch_mismatch") in rules

    def test_required_leaf_subject_is_missing_property(self):
        rules = _rules({"type": "object", "required": ["x", "y"]}, {})
        subjects = {subject for _, rule, subject in rules if rule == "required"}
        assert subjects == {canonical("x"), canonical("y")}

    def test_additional_properties_subject_is_value_independent(self):
        # 不同多余字段名必须同指纹
        r1 = _rules({"type": "object", "additionalProperties": False}, {"foo": 1})
        r2 = _rules({"type": "object", "additionalProperties": False}, {"bar": 2})
        assert r1 == r2
        assert r1[0][0] == "#" and r1[0][1] == "additionalProperties"


class TestProfileAggregation:
    SCHEMA = {
        "type": "object",
        "properties": {
            "a": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            "kind": {"oneOf": [
                {"type": "object", "required": ["x"],
                 "properties": {"x": {"type": "string"}}},
                {"type": "object", "required": ["y"],
                 "properties": {"y": {"type": "number"}}},
            ]},
            "name": {"type": "string"},
        },
        "required": ["name", "kind"],
        "additionalProperties": False,
    }

    DOCS = [
        {"name": "ok", "kind": {"x": "s"}},
        {"name": "bad", "kind": {"x": 1}, "a": [1]},
        {"name": 1, "kind": {"y": "no"}, "extra": 9},
        {"a": [9], "kind": {}},
    ]

    def test_counts_and_rate(self):
        result = profile_documents(self.SCHEMA, self.DOCS)
        assert result["total"] == 4
        assert result["valid"] == 1
        assert result["invalid"] == 3
        assert result["compliance_rate"] == 0.25

    def test_failures_grouped_by_pointer_and_rule(self):
        result = profile_documents(self.SCHEMA, self.DOCS)
        by_key = {(g["path"], g["validator"]): g for g in result["failures"]}
        # anyOf 同路径不同期望值 -> 合并为 #/a 的 type，2 篇文档命中
        merged = by_key[("#/a", "type")]
        assert merged["document_count"] == 2
        assert merged["example_indices"] == [1, 3]
        # oneOf 分叉 -> #/kind oneOf，2 篇文档命中
        assert by_key[("#/kind", "oneOf")]["document_count"] == 2
        # 不同缺失属性是不同 required 指纹
        assert ("#", "required") in by_key

    def test_grouping_order_independent_of_input_order(self):
        forward = profile_documents(self.SCHEMA, self.DOCS)
        backward = profile_documents(self.SCHEMA, list(reversed(self.DOCS)))
        keyf = lambda r: [(g["path"], g["validator"], g["fingerprint"],
                           g["document_count"]) for g in r["failures"]]
        assert keyf(forward) == keyf(backward)
        assert forward["compliance_rate"] == backward["compliance_rate"]
        # 示例下标随顺序变化（它们是提交列表下标），但都是升序且数量一致
        ex_f = [len(g["example_indices"]) for g in forward["failures"]]
        ex_b = [len(g["example_indices"]) for g in backward["failures"]]
        assert ex_f == ex_b
        for g in forward["failures"]:
            assert g["example_indices"] == sorted(g["example_indices"])

    def test_message_is_order_independent(self):
        # 归并组代表消息取字典序最小：正序 / 逆序结果一致
        forward = profile_documents(self.SCHEMA, self.DOCS)
        backward = profile_documents(self.SCHEMA, list(reversed(self.DOCS)))
        mf = {(g["path"], g["validator"]): g["message"] for g in forward["failures"]}
        mb = {(g["path"], g["validator"]): g["message"] for g in backward["failures"]}
        assert mf == mb

    def test_fingerprint_value_independent(self):
        # name 类型错误，不同取值应有同一指纹
        s = {"type": "object", "properties": {"name": {"type": "string"}},
             "required": ["name"]}
        r1 = profile_documents(s, [{"name": 1}, {"name": [1, 2]}, {"name": True}])
        type_groups = [g for g in r1["failures"] if g["path"] == "#/name"]
        assert len(type_groups) == 1
        assert type_groups[0]["document_count"] == 3

    def test_all_valid(self):
        result = profile_documents(self.SCHEMA, [self.DOCS[0]])
        assert result["valid"] == 1 and result["invalid"] == 0
        assert result["compliance_rate"] == 1.0
        assert result["failures"] == []

    def test_max_examples_capped_and_smallest_kept(self):
        # 12 篇相同失败文档，示例下标最多 10 个且保留最小的 10 个
        bad = [{"kind": {"z": 1}} for _ in range(12)]
        good = [{"name": "ok", "kind": {"x": "s"}}]
        result = profile_documents(self.SCHEMA, good + bad)
        kind = [g for g in result["failures"] if g["path"] == "#/kind"]
        assert kind and kind[0]["document_count"] == 12
        assert kind[0]["example_indices"] == list(range(1, 11))

    def test_limit_constant(self):
        assert MAX_PROFILE_DOCUMENTS == 5000
