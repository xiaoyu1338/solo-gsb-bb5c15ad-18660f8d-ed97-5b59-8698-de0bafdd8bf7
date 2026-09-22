"""FastAPI 接口测试（TestClient + 临时 SQLite）。"""
import os
import tempfile

import pytest

# 在导入应用前指定临时数据库
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


OLD_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled", "pending"]},
    },
}
NEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled"]},
        "owner": {"type": "string"},
    },
    "required": ["status", "owner"],
}


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_and_get_analysis(client):
    resp = client.post("/api/analyses", json={
        "name": "user-schema-v1-v2",
        "old_schema": OLD_SCHEMA,
        "new_schema": NEW_SCHEMA,
        "samples": [{"status": "pending"}],
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    analysis_id = data["id"]
    assert data["report"]["backward_compatible"] is False
    assert data["report"]["forward_compatible"] is True
    assert data["report"]["summary"]["errors"] >= 1

    # 反例字段存在
    issues = data["report"]["issues"]
    backward = [i for i in issues if i["direction"] == "backward"]
    assert backward
    assert any(i["counterexample"] is not None for i in backward)

    # 样例结果
    assert data["report"]["sample_results"][0]["valid_against_new"] is False

    # 查询报告
    resp2 = client.get(f"/api/analyses/{analysis_id}")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == analysis_id

    # 列表
    resp3 = client.get("/api/analyses")
    assert resp3.status_code == 200
    assert resp3.json()["total"] >= 1


def test_get_missing_returns_404(client):
    resp = client.get("/api/analyses/999999")
    assert resp.status_code == 404


def test_invalid_schema_returns_400(client):
    resp = client.post("/api/analyses", json={
        "old_schema": {"type": "not-a-real-type"},
        "new_schema": {"type": "string"},
    })
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["field"] == "old_schema"


def test_compatible_schemas(client):
    resp = client.post("/api/analyses", json={
        "old_schema": {"type": "object", "properties": {"a": {"type": "string"}}},
        "new_schema": {"type": "object",
                       "properties": {"a": {"type": "string"},
                                      "b": {"type": "integer"}}},
    })
    assert resp.status_code == 201
    report = resp.json()["report"]
    assert report["backward_compatible"]
    assert report["forward_compatible"]
    assert report["summary"]["errors"] == 0


def test_conditional_if_then_else_end_to_end(client):
    """draft-07+ 条件分支：then 收紧 -> 向后破坏，issue 定位到 #/then。"""
    if_a = {"required": ["kind"], "properties": {"kind": {"const": "a"}}}
    old = {"type": "object",
           "properties": {"kind": {"enum": ["a", "b"]}},
           "required": ["kind"],
           "if": if_a, "then": {"required": ["x"]}}
    new = {"type": "object",
           "properties": {"kind": {"enum": ["a", "b"]}},
           "required": ["kind"],
           "if": if_a, "then": {"required": ["x", "y"]}}
    resp = client.post("/api/analyses", json={
        "name": "conditional", "old_schema": old, "new_schema": new,
    })
    assert resp.status_code == 201, resp.text
    report = resp.json()["report"]
    assert report["backward_compatible"] is False
    assert report["forward_compatible"] is True
    issue = [i for i in report["issues"] if i["direction"] == "backward"][0]
    assert issue["code"] == "required_added"
    assert issue["path"] == "#/then"
    assert issue["severity"] == "error"
    assert issue["counterexample"]["kind"] == "a"


# ---------------------------------------------------------------------------
# POST /api/analyses/compare
# ---------------------------------------------------------------------------

COMPARE_OLD = {
    "type": "object",
    "properties": {"a": {"type": "string"}},
    "required": ["a"],
}
# v2：a 收紧 minLength、新增必填 b
COMPARE_V2 = {
    "type": "object",
    "properties": {"a": {"type": "string", "minLength": 2},
                   "b": {"type": "integer"}},
    "required": ["a", "b"],
}
# v3：b 不再必填（修复）、新增必填 c（新引入）、a 的 minLength 保持（仍存在）
COMPARE_V3 = {
    "type": "object",
    "properties": {"a": {"type": "string", "minLength": 2},
                   "c": {"type": "string"}},
    "required": ["a", "c"],
}


def _create(client, old_schema, new_schema, name=None):
    resp = client.post("/api/analyses", json={
        "name": name, "old_schema": old_schema, "new_schema": new_schema,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_compare_added_fixed_persisting(client):
    baseline = _create(client, COMPARE_OLD, COMPARE_V2, "baseline")
    current = _create(client, COMPARE_OLD, COMPARE_V3, "current")

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": baseline["id"], "current_id": current["id"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["baseline_id"] == baseline["id"]
    assert data["current_id"] == current["id"]
    assert data["counts"] == {"added": 1, "fixed": 1, "persisting": 1,
                              "severity_changed": 0}

    added = data["issues"]["added"]
    fixed = data["issues"]["fixed"]
    persisting = data["issues"]["persisting"]
    assert [i["code"] for i in added] == ["required_added"]
    assert "'c'" in added[0]["reason"]
    assert [i["code"] for i in fixed] == ["required_added"]
    assert "'b'" in fixed[0]["reason"]
    assert [i["code"] for i in persisting] == ["min_length_increased"]

    # 每条对比 issue 都带稳定指纹
    for group in (added, fixed, persisting):
        for entry in group:
            assert len(entry["fingerprint"]) == 16

    # 双向兼容性：两个方向都保持不兼容 -> changed 为 False
    assert data["compatibility"]["backward"] == {
        "baseline": False, "current": False, "changed": False,
    }
    assert data["compatibility"]["forward"] == {
        "baseline": True, "current": True, "changed": False,
    }

    # 错误 / 警告汇总差异
    diff = data["summary_diff"]
    assert diff["errors"]["baseline"] == baseline["report"]["summary"]["errors"]
    assert diff["errors"]["current"] == current["report"]["summary"]["errors"]
    assert diff["errors"]["delta"] == (
        current["report"]["summary"]["errors"]
        - baseline["report"]["summary"]["errors"]
    )
    assert diff["warnings"]["delta"] == 0
    assert diff["backward"]["errors"]["baseline"] == 2


def test_compare_severity_only_change(client):
    # 同一 integer_required 问题：baseline 可构造验证反例（error），
    # current 中 maximum 限制使反例不再成立（降级 warning）
    baseline = _create(client, {"minimum": 0}, {"type": "integer"})
    current = _create(client, {"minimum": 0, "maximum": 1}, {"type": "integer"})

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": baseline["id"], "current_id": current["id"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()

    changed = data["issues"]["severity_changed"]
    assert data["counts"]["severity_changed"] == 1
    assert changed[0]["code"] == "integer_required"
    assert changed[0]["baseline_severity"] == "error"
    assert changed[0]["current_severity"] == "warning"
    # 仍存在：type_restricted 两份报告都有
    assert any(i["code"] == "type_restricted" for i in data["issues"]["persisting"])


def test_compare_does_not_modify_reports(client):
    baseline = _create(client, COMPARE_OLD, COMPARE_V2)
    current = _create(client, COMPARE_OLD, COMPARE_V3)

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": baseline["id"], "current_id": current["id"],
    })
    assert resp.status_code == 200

    for original in (baseline, current):
        after = client.get(f"/api/analyses/{original['id']}").json()
        assert after["report"] == original["report"]
        assert after["old_schema"] == original["old_schema"]
        assert after["new_schema"] == original["new_schema"]


def test_compare_self_all_persisting(client):
    analysis = _create(client, COMPARE_OLD, COMPARE_V2)
    resp = client.post("/api/analyses/compare", json={
        "baseline_id": analysis["id"], "current_id": analysis["id"],
    })
    assert resp.status_code == 200
    data = resp.json()
    total = analysis["report"]["summary"]["total"]
    assert data["counts"]["persisting"] == total
    assert data["counts"]["added"] == 0
    assert data["counts"]["fixed"] == 0


def test_compare_missing_analysis_returns_404(client):
    analysis = _create(client, COMPARE_OLD, COMPARE_V2)

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": 999999, "current_id": analysis["id"],
    })
    assert resp.status_code == 404

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": analysis["id"], "current_id": 999999,
    })
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/gate
# ---------------------------------------------------------------------------

# backward：required_added error（#）+ 两条 pattern_changed warning
# forward：两条 pattern_changed warning
GATE_OLD = {
    "type": "object",
    "properties": {
        "a": {"type": "string", "pattern": "^x"},
        "b": {"type": "string", "pattern": "^y"},
    },
}
GATE_NEW = {
    "type": "object",
    "properties": {
        "a": {"type": "string", "pattern": "^xx"},
        "b": {"type": "string", "pattern": "^yy"},
    },
    "required": ["b"],
}


def _gate_report(client):
    return _create(client, GATE_OLD, GATE_NEW, "gate-case")


def test_gate_error_blocks_and_counts(client):
    analysis = _gate_report(client)
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pass"] is False
    counts = data["counts"]["backward"]
    assert counts["errors"] == 1
    assert counts["warnings"] == 2
    assert counts["exempted"] == 0
    assert counts["total"] == 3
    assert counts["max_warnings"] == 0
    codes = [(i["code"], i["blocked_reason"]) for i in data["blocking"]]
    assert ("required_added", "error") in codes
    assert sum(1 for _, reason in codes if reason == "warning_limit_exceeded") == 2
    assert data["exempted"] == []


def test_gate_warning_cap_pass_and_overflow(client):
    analysis = _gate_report(client)
    # cap=1：required error 豁免后只剩 2 条 warning，超 1 条
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "max_warnings": {"backward": 1},
        "exemptions": [
            {"direction": "backward", "code": "required_added", "path": "#"},
        ],
    })
    data = resp.json()
    assert data["pass"] is False
    assert [b["path"] for b in data["blocking"]] == ["#/properties/b"]
    assert data["blocking"][0]["blocked_reason"] == "warning_limit_exceeded"
    assert data["counts"]["backward"]["warnings"] == 2
    assert data["counts"]["backward"]["errors"] == 0
    assert data["exempted"][0]["rule_index"] == 0

    # cap=2：全部 warning 在额度内 -> 通过
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "max_warnings": {"backward": 2},
        "exemptions": [
            {"direction": "backward", "code": "required_added", "path": "#"},
        ],
    })
    assert resp.json()["pass"] is True
    assert resp.json()["blocking"] == []


def test_gate_wildcard_exemption_and_rule_index(client):
    analysis = _gate_report(client)
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "exemptions": [
            # 下标 0：通配规则命中两条 pattern_changed
            {"direction": "backward", "code": "pattern_changed",
             "path": "#/properties/*"},
            # 下标 1：精确规则命中 required_added
            {"direction": "backward", "code": "required_added", "path": "#"},
        ],
    })
    data = resp.json()
    assert data["pass"] is True
    rule_indices = sorted((e["path"], e["rule_index"]) for e in data["exempted"])
    assert rule_indices == [
        ("#", 1),
        ("#/properties/a", 0),
        ("#/properties/b", 0),
    ]
    # 豁免项不计数
    assert data["counts"]["backward"] == {
        "errors": 0, "warnings": 0, "exempted": 3, "total": 3,
        "max_warnings": 0,
    }


def test_gate_exact_rule_beats_later_wildcard(client):
    analysis = _gate_report(client)
    # 两条规则都命中 #/properties/a 的 pattern_changed：
    # 下标 0 精确 path（3 精确字段），下标 1 通配 path（2 精确字段）；
    # 即使通配规则声明更晚，也必须记到精确规则。
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "exemptions": [
            {"direction": "backward", "code": "pattern_changed",
             "path": "#/properties/a"},
            {"direction": "backward", "code": "pattern_changed",
             "path": "#/properties/*"},
        ],
    })
    data = resp.json()
    hit_a = [e for e in data["exempted"] if e["path"] == "#/properties/a"]
    hit_b = [e for e in data["exempted"] if e["path"] == "#/properties/b"]
    assert len(hit_a) == 1 and hit_a[0]["rule_index"] == 0
    assert len(hit_b) == 1 and hit_b[0]["rule_index"] == 1
    # required_added 未被豁免 -> 门禁仍失败
    assert data["pass"] is False


def test_gate_unchecked_direction_ignored(client):
    analysis = _gate_report(client)
    # 只检查 forward：backward 的 error 完全不影响结论；forward 2 条 warning，cap=2
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["forward"],
        "max_warnings": {"forward": 2, "backward": 0},
    })
    data = resp.json()
    assert data["pass"] is True
    assert set(data["counts"]) == {"forward"}
    assert data["counts"]["forward"]["warnings"] == 2


def test_gate_clean_direction_passes(client):
    # 新增可选字段：两个方向都没有 issue
    analysis = _create(
        client,
        {"type": "object", "properties": {"a": {"type": "string"}}},
        {"type": "object", "properties": {"a": {"type": "string"},
                                           "b": {"type": "integer"}}},
    )
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward", "forward"],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["pass"] is True
    assert data["blocking"] == []
    assert data["exempted"] == []
    assert data["counts"]["backward"]["total"] == 0
    assert data["counts"]["forward"]["total"] == 0


def test_gate_missing_analysis_returns_404(client):
    resp = client.post("/api/analyses/999999/gate", json={
        "directions": ["backward"],
    })
    assert resp.status_code == 404
    assert resp.json()["detail"] == "分析记录不存在"


def test_gate_validation_errors(client):
    analysis = _gate_report(client)
    base = f"/api/analyses/{analysis['id']}/gate"

    # directions 不能为空
    resp = client.post(base, json={"directions": []})
    assert resp.status_code == 422

    # 非法方向
    resp = client.post(base, json={"directions": ["sideways"]})
    assert resp.status_code == 422

    # 非法 path（不以 #/ 开头）
    resp = client.post(base, json={
        "directions": ["backward"],
        "exemptions": [{"direction": "backward", "code": "x", "path": "properties/a"}],
    })
    assert resp.status_code == 422

    # 负的 warning 上限
    resp = client.post(base, json={
        "directions": ["backward"], "max_warnings": {"backward": -1},
    })
    assert resp.status_code == 422

    # code 不能为空
    resp = client.post(base, json={
        "directions": ["backward"],
        "exemptions": [{"direction": "backward", "code": "", "path": "#"}],
    })
    assert resp.status_code == 422


def test_gate_does_not_modify_report(client):
    analysis = _gate_report(client)
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "exemptions": [
            {"direction": "backward", "code": "required_added", "path": "#"},
            {"direction": "backward", "code": "pattern_changed",
             "path": "#/properties/*"},
        ],
    })
    assert resp.status_code == 200

    after = client.get(f"/api/analyses/{analysis['id']}").json()
    assert after["report"] == analysis["report"]
    assert after["old_schema"] == analysis["old_schema"]
    assert after["new_schema"] == analysis["new_schema"]


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/profile 文档画像
# ---------------------------------------------------------------------------

PROFILE_SCHEMA = {
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


def _profile_analysis(client):
    # new_schema 即画像使用的已保存新版 schema
    return _create(client,
                   {"type": "object"},
                   PROFILE_SCHEMA,
                   "profile-case")


PROFILE_DOCS = [
    {"name": "ok", "kind": {"x": "s"}},
    {"name": "bad", "kind": {"x": 1}, "a": [1]},
    {"name": 1, "kind": {"y": "no"}, "extra": 9},
    {"a": [9], "kind": {}},
]


def test_profile_create_and_get(client):
    analysis = _profile_analysis(client)
    resp = client.post(f"/api/analyses/{analysis['id']}/profile",
                       json={"documents": PROFILE_DOCS})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["analysis_id"] == analysis["id"]
    assert data["total"] == 4
    assert data["valid"] == 1
    assert data["invalid"] == 3
    assert data["compliance_rate"] == 0.25
    assert "created_at" in data

    by_key = {(g["path"], g["validator"]): g for g in data["failures"]}
    # anyOf 同路径不同期望 -> 合并为 #/a 的 type
    merged = by_key[("#/a", "type")]
    assert merged["document_count"] == 2
    assert merged["example_indices"] == [1, 3]
    assert len(merged["fingerprint"]) == 16
    # oneOf 分叉 -> #/kind oneOf
    assert by_key[("#/kind", "oneOf")]["document_count"] == 2
    # 每个组都有非空代表消息
    assert all(g["message"] for g in data["failures"])

    # 分组顺序稳定：按 (path, validator, fingerprint)
    ordered = [(g["path"], g["validator"], g["fingerprint"]) for g in data["failures"]]
    assert ordered == sorted(ordered)

    # GET 查询到同一份持久化快照
    resp2 = client.get(f"/api/analyses/{analysis['id']}/profile")
    assert resp2.status_code == 200, resp2.text
    assert resp2.json() == data


def test_profile_uses_saved_new_schema(client):
    # 画像只针对已保存的 new_schema：old 允许任意对象、new 要求必填 z
    analysis = _create(client,
                       {"type": "object"},
                       {"type": "object", "required": ["z"]})
    resp = client.post(f"/api/analyses/{analysis['id']}/profile",
                       json={"documents": [{"other": 1}]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] == 0 and data["invalid"] == 1
    assert data["failures"][0]["validator"] == "required"
    # 满足 new_schema 的对象通过
    ok = client.post(f"/api/analyses/{analysis['id']}/profile",
                     json={"documents": [{"z": 1}]}).json()
    assert ok["valid"] == 1


def test_profile_order_does_not_affect_groups(client):
    analysis = _profile_analysis(client)
    r1 = client.post(f"/api/analyses/{analysis['id']}/profile",
                     json={"documents": PROFILE_DOCS}).json()
    r2 = client.post(f"/api/analyses/{analysis['id']}/profile",
                     json={"documents": list(reversed(PROFILE_DOCS))}).json()
    keyf = lambda r: [(g["path"], g["validator"], g["fingerprint"],
                       g["document_count"]) for g in r["failures"]]
    assert keyf(r1) == keyf(r2)
    assert r1["compliance_rate"] == r2["compliance_rate"]


def test_profile_upsert_replaces_snapshot(client):
    analysis = _profile_analysis(client)
    client.post(f"/api/analyses/{analysis['id']}/profile",
                json={"documents": PROFILE_DOCS})
    # 再次提交：覆盖而非新增
    resp = client.post(f"/api/analyses/{analysis['id']}/profile",
                       json={"documents": [PROFILE_DOCS[0]]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1 and data["valid"] == 1
    assert data["failures"] == []
    # GET 读到的是被覆盖后的最新快照
    assert client.get(f"/api/analyses/{analysis['id']}/profile").json() == data


def test_profile_get_404_when_missing_analysis(client):
    assert client.post("/api/analyses/999999/profile",
                       json={"documents": [{}]}).status_code == 404
    assert client.get("/api/analyses/999999/profile").status_code == 404


def test_profile_get_404_when_no_snapshot(client):
    analysis = _profile_analysis(client)
    resp = client.get(f"/api/analyses/{analysis['id']}/profile")
    assert resp.status_code == 404
    assert "画像" in resp.json()["detail"]


def test_profile_over_limit_returns_422_and_does_not_write(client):
    analysis = _profile_analysis(client)

    # 空列表 -> 422
    assert client.post(f"/api/analyses/{analysis['id']}/profile",
                       json={"documents": []}).status_code == 422

    # 超过 5000 -> 422
    too_many = [{"name": "x", "kind": {"x": "s"}} for _ in range(5001)]
    assert client.post(f"/api/analyses/{analysis['id']}/profile",
                       json={"documents": too_many}).status_code == 422

    # 非对象文档（数字 / 字符串 / 数组 / null）-> 422
    for bad in ([1], ["s"], [[1]], [None]):
        assert client.post(f"/api/analyses/{analysis['id']}/profile",
                           json={"documents": bad}).status_code == 422

    # 全部被拒：分析记录仍在，但画像快照未写入
    assert client.get(f"/api/analyses/{analysis['id']}/profile").status_code == 404


def test_profile_boundary_5000_accepted(client):
    analysis = _profile_analysis(client)
    docs = [{"name": "x", "kind": {"x": "s"}} for _ in range(5000)]
    resp = client.post(f"/api/analyses/{analysis['id']}/profile",
                       json={"documents": docs})
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 5000
    assert resp.json()["valid"] == 5000
    assert resp.json()["compliance_rate"] == 1.0


def test_profile_does_not_affect_other_endpoints(client):
    # 画像写入后，分析报告 / SARIF / 门禁仍可正常读取，且报告未被修改
    analysis = _profile_analysis(client)
    before = client.get(f"/api/analyses/{analysis['id']}").json()
    client.post(f"/api/analyses/{analysis['id']}/profile",
                json={"documents": PROFILE_DOCS})

    after = client.get(f"/api/analyses/{analysis['id']}").json()
    assert after["report"] == before["report"]
    assert client.get(f"/api/analyses/{analysis['id']}/sarif").status_code == 200
    assert client.post(f"/api/analyses/{analysis['id']}/gate",
                       json={"directions": ["backward"]}).status_code == 200
