#!/usr/bin/env python3
"""restore.py 核心逻辑单元测试（仅标准库）。"""
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import make_sample as sample
from restore import (FullFile, IllegalOp, Increment, Restorer, RestoreFailure,
                     apply_ops, canonical_text, digest_rows, parse_full,
                     parse_increment)


class Counter:
    """计数加载器：证明同次恢复复用已校验的中间版。"""

    def __init__(self, text):
        self.text = text
        self.n = 0

    def __call__(self):
        self.n += 1
        return self.text


class TestCanonical(unittest.TestCase):
    def test_sorted_compact_keep_non_ascii(self):
        rows = [["P0010", "王家洼", "王家乡"], ["P0002", "李家屯", "李家乡"]]
        text = canonical_text(rows)
        # 编号文本升序 + 紧凑分隔符 + 保留非 ASCII
        self.assertEqual(
            text, '[["P0002","李家屯","李家乡"],["P0010","王家洼","王家乡"]]')
        self.assertIn("李家屯", text)
        self.assertNotIn(", ", text)

    def test_text_ascending_not_numeric(self):
        rows = [["P2", "乙", "某乡"], ["P10", "甲", "某乡"]]
        ids = [r[0] for r in json.loads(canonical_text(rows))]
        self.assertEqual(ids, ["P10", "P2"])  # 文本升序：'1' < '2'

    def test_digest_matches_manual_sha256(self):
        rows = sample.V1_ROWS
        expect = hashlib.sha256(canonical_text(rows).encode("utf-8")).hexdigest()
        self.assertEqual(digest_rows(rows), expect)

    def test_sample_digests_match_core_algorithm(self):
        # 样例包生成器与核心算法必须一致，否则清单核对必然失败
        self.assertEqual(sample.D1, digest_rows(sample.V1_ROWS))
        self.assertEqual(sample.D2, digest_rows(sample.V2_ROWS))
        self.assertEqual(sample.D3, digest_rows(sample.V3_ROWS))


class TestParse(unittest.TestCase):
    def test_parse_full_array_and_object_rows(self):
        rows = parse_full('[["P1","甲村","甲乡"],'
                          '{"id":"P2","name":"乙村","village":"乙乡"}]', "测试")
        self.assertEqual(rows, [["P1", "甲村", "甲乡"], ["P2", "乙村", "乙乡"]])

    def test_parse_full_duplicate_id_rejected(self):
        with self.assertRaises(ValueError):
            parse_full('[["P1","甲","某乡"],["P1","乙","某乡"]]', "测试")

    def test_parse_full_bad_json(self):
        with self.assertRaises(ValueError):
            parse_full("{not json", "测试")

    def test_parse_increment_dict_form(self):
        ops = parse_increment(
            '{"add":[["P1","甲","某乡"]],"modify":[],"delete":["P2"]}', "测试")
        self.assertEqual(ops, [("add", ["P1", "甲", "某乡"]), ("delete", "P2")])

    def test_parse_increment_list_form(self):
        ops = parse_increment(
            '[{"op":"delete","id":"P2"},{"op":"add","row":["P1","甲","某乡"]}]',
            "测试")
        self.assertEqual(ops, [("delete", "P2"), ("add", ["P1", "甲", "某乡"])])

    def test_parse_increment_unknown_op(self):
        with self.assertRaises(ValueError):
            parse_increment('[{"op":"teleport","row":["P1","甲","某乡"]}]', "测试")


class TestApplyOps(unittest.TestCase):
    def setUp(self):
        self.state = {"P1": ["P1", "甲村", "甲乡"]}

    def test_legal_ops(self):
        out = apply_ops(self.state, [
            ("add", ["P2", "乙村", "乙乡"]),
            ("modify", ["P1", "甲村", "新甲乡"]),
            ("delete", "P2"),
        ], "测试")
        self.assertEqual(out, {"P1": ["P1", "甲村", "新甲乡"]})
        self.assertEqual(len(self.state), 1)  # 原状态不被改动

    def test_add_must_use_new_id(self):
        with self.assertRaises(IllegalOp):
            apply_ops(self.state, [("add", ["P1", "丙村", "丙乡"])], "测试")

    def test_modify_needs_existing(self):
        with self.assertRaises(IllegalOp):
            apply_ops(self.state, [("modify", ["P9", "丙村", "丙乡"])], "测试")

    def test_delete_needs_existing(self):
        with self.assertRaises(IllegalOp):
            apply_ops(self.state, [("delete", "P9")], "测试")


class TestRestorer(unittest.TestCase):
    def setUp(self):
        self.digests = {"v1": {sample.D1}, "v2": {sample.D2}, "v3": {sample.D3}}

    def _full(self, version, rows, label="full", present=True):
        c = Counter(json.dumps(rows, ensure_ascii=False))
        return FullFile(version, label, present, c if present else None), c

    def _inc(self, frm, to, inc, label="inc", present=True):
        c = Counter(json.dumps(inc, ensure_ascii=False))
        return Increment(frm, to, label, present, c if present else None), c

    def test_full_only(self):
        full, _ = self._full("v1", sample.V1_ROWS)
        state, steps = Restorer(self.digests, [full], []).restore("v1")
        self.assertEqual(sorted(state.values()), sorted(sample.V1_ROWS))
        self.assertEqual([s[0] for s in steps], ["full"])

    def test_chain_with_intermediate_reuse(self):
        full, fc = self._full("v1", sample.V1_ROWS)
        inc12, c12 = self._inc("v1", "v2", sample.INC12)
        bad, cbad = self._inc("v2", "v3",
                              {"add": [], "modify": [["P0001", "错", "错乡"]],
                               "delete": []}, label="bad")
        good, cgood = self._inc("v2", "v3", sample.INC23, label="good")
        r = Restorer(self.digests, [full], [inc12, bad, good])
        state, steps = r.restore("v3")
        self.assertEqual(sorted(state.values()), sorted(sample.V3_ROWS))
        self.assertEqual([s[0] for s in steps],
                         ["full", "increment", "increment"])
        self.assertEqual(steps[-1][1], "good")
        # 同次恢复复用已校验的中间版：v1、v2 各自只还原了一次
        self.assertEqual((fc.n, c12.n), (1, 1))
        self.assertEqual((cbad.n, cgood.n), (1, 1))
        # 缓存以 (版号, 摘要) 为键：同版不同摘要的名录互不顶替
        self.assertIn(("v1", sample.D1), r.verified)
        self.assertIn(("v2", sample.D2), r.verified)

    def test_missing_package_reports_and_fails(self):
        full, _ = self._full("v1", sample.V1_ROWS)
        inc12, _ = self._inc("v1", "v2", sample.INC12, present=False)
        with self.assertRaises(RestoreFailure) as ctx:
            Restorer(self.digests, [full], [inc12]).restore("v2")
        self.assertTrue(any("缺少增量文件" in r for r in ctx.exception.reasons))

    def test_illegal_op_reports_and_fails(self):
        full, _ = self._full("v1", sample.V1_ROWS)
        inc12, _ = self._inc("v1", "v2", sample.INC12_ILLEGAL)
        with self.assertRaises(RestoreFailure) as ctx:
            Restorer(self.digests, [full], [inc12]).restore("v2")
        self.assertTrue(any("没有原记录" in r for r in ctx.exception.reasons))

    def test_digest_mismatch_reports_and_fails(self):
        full, _ = self._full("v1", sample.V1_ROWS)
        inc12, _ = self._inc("v1", "v2", sample.INC12_WRONG)
        with self.assertRaises(RestoreFailure) as ctx:
            Restorer(self.digests, [full], [inc12]).restore("v2")
        self.assertTrue(any("摘要不符" in r for r in ctx.exception.reasons))

    def test_cycle_detected(self):
        inc12, _ = self._inc("v1", "v2", sample.INC12)
        inc21, _ = self._inc("v2", "v1", sample.INC12)
        with self.assertRaises(RestoreFailure) as ctx:
            Restorer(self.digests, [], [inc12, inc21]).restore("v2")
        self.assertTrue(any("循环" in r for r in ctx.exception.reasons))

    def test_no_chain_at_all(self):
        with self.assertRaises(RestoreFailure) as ctx:
            Restorer(self.digests, [], []).restore("v3")
        self.assertTrue(any("没有全量文件" in r for r in ctx.exception.reasons))

    def test_version_without_digest_cannot_verify(self):
        full, _ = self._full("v9", sample.V1_ROWS)
        with self.assertRaises(RestoreFailure) as ctx:
            Restorer(self.digests, [full], []).restore("v9")
        self.assertTrue(any("无法核对" in r for r in ctx.exception.reasons))

    # ---------------- 同一版两份清单、摘要各不相同 ----------------
    def test_two_manifests_falls_back_to_other_lineage(self):
        """A 清单先校验通过的 v2 名录接不上更名包时，改用 B 清单的 v2 名录。"""
        d2A = sample.D2
        d2B = digest_rows([r for r in sample.V2_ROWS if r[0] != "P0011"])
        # 两份清单对目标版 v3 登记同一个摘要
        scope_a = {"v1": {sample.D1}, "v2": {d2A}, "v3": {sample.D3}}
        scope_b = {"v2": {d2B}, "v3": {sample.D3}}

        full_v1, _ = self._full("v1", sample.V1_ROWS, label="A/full_v1")
        full_v2_a, _ = self._full("v2", sample.V2_ROWS, label="A/full_v2")
        inc12, _ = self._inc("v1", "v2", sample.INC12, label="A/inc12")
        inc23_bad, _ = self._inc(
            "v2", "v3", {"add": [], "modify": [], "delete": ["P9999"]},
            label="A/inc23_illegal")
        # B 的 v2 是另一份名录；它的增量只能作用在 B 的 v2 上
        v2B = [r for r in sample.V2_ROWS if r[0] != "P0011"]
        inc23_from_B = {
            "add": [["P0011", "赵家堡", "赵家乡"],
                    ["P0012", "孙家沟", "孙家乡"]],
            "modify": [["P0001", "张家庄", "张家乡"]], "delete": []}
        full_v2_b, _ = self._full("v2", v2B, label="B/full_v2")
        inc23_good, cgood = self._inc("v2", "v3", inc23_from_B,
                                      label="B/inc23_good")
        for f, sc in ((full_v1, scope_a), (full_v2_a, scope_a),
                      (inc12, scope_a), (inc23_bad, scope_a)):
            f.digest_scope = sc
        for f in (full_v2_b, inc23_good):
            f.digest_scope = scope_b

        digests = {"v1": {sample.D1}, "v2": {d2A, d2B}, "v3": {sample.D3}}
        r = Restorer(digests,
                     [full_v1, full_v2_a, full_v2_b],
                     [inc12, inc23_bad, inc23_good])
        state, steps = r.restore("v3")
        self.assertEqual(sorted(state.values()), sorted(sample.V3_ROWS))
        # 实际用到的是 B 清单的链
        self.assertEqual([s[1] for s in steps],
                         ["B/full_v2", "B/inc23_good"])
        # 同版不同摘要分别还原、各自缓存
        self.assertIn(("v2", d2A), r.verified)
        self.assertIn(("v2", d2B), r.verified)
        self.assertEqual(cgood.n, 1)

    def test_same_version_same_digest_reused_across_increments(self):
        """同版同摘要的中间名录尝试多个后续增量时，只还原一次。"""
        scope = {"v1": {sample.D1}, "v2": {sample.D2}, "v3": {sample.D3}}
        full, fc = self._full("v1", sample.V1_ROWS, label="full")
        inc12, c12 = self._inc("v1", "v2", sample.INC12, label="inc12")
        bad, _ = self._inc(
            "v2", "v3",
            {"add": [], "modify": [["P0001", "错", "错乡"]], "delete": []},
            label="bad")
        good, cgood = self._inc("v2", "v3", sample.INC23, label="good")
        for f in (full, inc12, bad, good):
            f.digest_scope = scope
        r = Restorer(self.digests, [full], [inc12, bad, good])
        state, _ = r.restore("v3")
        self.assertEqual(sorted(state.values()), sorted(sample.V3_ROWS))
        # v2 同摘要，试两个后续增量时整表/首增量都只加载一次
        self.assertEqual((fc.n, c12.n, cgood.n), (1, 1, 1))

    def test_manifests_do_not_cross_check_each_other(self):
        """甲清单的 v2 摘要不能拿去核对乙清单还原出的 v2。"""
        d2A = sample.D2
        d2B = digest_rows([r for r in sample.V2_ROWS if r[0] != "P0011"])
        scope_a = {"v2": {d2A}}
        scope_b = {"v2": {d2B}}
        v2B = [r for r in sample.V2_ROWS if r[0] != "P0011"]
        full_b, _ = self._full("v2", v2B, label="B/full_v2")
        full_b.digest_scope = scope_b
        # 全局池里同时有两个摘要；核对时只能用 B 自己的 d2B
        r = Restorer({"v2": {d2A, d2B}}, [full_b], [])
        state, _ = r.restore("v2")
        self.assertEqual(sorted(state.values()), sorted(v2B))
        self.assertNotIn(("v2", d2A), r.verified)
        self.assertIn(("v2", d2B), r.verified)


if __name__ == "__main__":
    unittest.main()
