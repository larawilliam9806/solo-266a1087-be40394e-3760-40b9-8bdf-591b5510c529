#!/usr/bin/env python3
"""Flask 补档页端到端测试：上传 ZIP → 选版恢复 → 名录与恢复记录。"""
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import make_sample as sample
from app import create_app


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.db_path = str(d / "app.db")
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": self.db_path,
            "UPLOAD_DIR": str(d / "uploads"),
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------ 辅助
    def upload(self, data, name="backup.zip"):
        return self.client.post(
            "/upload", data={"file": (io.BytesIO(data), name)},
            content_type="multipart/form-data", follow_redirects=True)

    def restore(self, version):
        return self.client.post("/restore", data={"version": version},
                                follow_redirects=True)

    def gazetteer(self):
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT id, name, village FROM gazetteer ORDER BY id").fetchall()
        con.close()
        return [list(r) for r in rows]

    def query(self, sql, args=()):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, args).fetchall()
        con.close()
        return rows

    def meta(self, key):
        rows = self.query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    # ------------------------------------------------------------ 用例
    def test_index_empty(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("尚未恢复".encode(), r.data)

    def test_upload_rejects_non_zip(self):
        r = self.upload(b"this is not a zip", "bad.zip")
        self.assertIn("不是有效的 ZIP".encode(), r.data)

    def test_upload_rejects_missing_manifest(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("full_v1.json", "[]")
        r = self.upload(buf.getvalue())
        self.assertIn("未找到清单".encode(), r.data)

    def test_good_chain_restore_v3(self):
        r = self.upload(sample.good_zip(), "good.zip")
        self.assertIn("已接收备份包".encode(), r.data)

        r = self.restore("v3")
        self.assertIn("已恢复到版本 v3".encode(), r.data)
        self.assertEqual(self.gazetteer(), sorted(sample.V3_ROWS))
        self.assertEqual(self.meta("current_version"), "v3")

        rec = self.query("SELECT * FROM recoveries")
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["status"], "success")
        files = self.query("SELECT * FROM recovery_files WHERE recovery_id = ?",
                           (rec[0]["id"],))
        self.assertEqual(len(files), 3)  # 1 整表 + 2 增量
        self.assertEqual([f["kind"] for f in files],
                         ["full", "increment", "increment"])
        self.assertEqual(files[1]["version_span"], "v1→v2")
        self.assertEqual(files[2]["version_span"], "v2→v3")

    def test_restore_v1_from_full_only(self):
        self.upload(sample.good_zip())
        r = self.restore("v1")
        self.assertIn("已恢复到版本 v1".encode(), r.data)
        self.assertEqual(self.gazetteer(), sorted(sample.V1_ROWS))

    def test_missing_package_blocks_v3_but_v2_ok(self):
        r = self.upload(sample.missing_zip(), "missing.zip")
        self.assertIn("包内缺失".encode(), r.data)

        r = self.restore("v3")
        self.assertIn("缺少增量文件".encode(), r.data)
        self.assertEqual(self.gazetteer(), [])  # 原名录不动
        rec = self.query("SELECT * FROM recoveries WHERE status = 'failed'")
        self.assertEqual(len(rec), 1)
        self.assertIn("缺少增量文件", rec[0]["reason"])

        # 完整的那一段链仍然可用
        r = self.restore("v2")
        self.assertIn("已恢复到版本 v2".encode(), r.data)
        self.assertEqual(self.gazetteer(), sorted(sample.V2_ROWS))

    def test_illegal_increment_rejected(self):
        self.upload(sample.badops_zip(), "badops.zip")
        r = self.restore("v2")
        self.assertIn("没有原记录".encode(), r.data)
        self.assertEqual(self.gazetteer(), [])

    def test_multichain_falls_back_to_valid_increment(self):
        self.upload(sample.multichain_zip(), "multi.zip")
        r = self.restore("v3")
        self.assertIn("已恢复到版本 v3".encode(), r.data)
        self.assertEqual(self.gazetteer(), sorted(sample.V3_ROWS))
        rec = self.query("SELECT * FROM recoveries WHERE status = 'success'")
        files = self.query("SELECT * FROM recovery_files WHERE recovery_id = ?",
                           (rec[0]["id"],))
        sources = " ".join(f["source"] for f in files)
        self.assertIn("inc_v1_v2.json", sources)
        self.assertNotIn("inc_v1_v2_bad.json", sources)

    def test_restore_from_later_full_file(self):
        self.upload(sample.fullv2_zip(), "fullv2.zip")
        r = self.restore("v3")
        self.assertIn("已恢复到版本 v3".encode(), r.data)
        self.assertEqual(self.gazetteer(), sorted(sample.V3_ROWS))
        rec = self.query("SELECT * FROM recoveries WHERE status = 'success'")
        files = self.query("SELECT * FROM recovery_files WHERE recovery_id = ?",
                           (rec[0]["id"],))
        self.assertEqual([f["kind"] for f in files], ["full", "increment"])
        self.assertEqual(files[0]["version_span"], "v2")

    def test_failed_restore_keeps_previous_gazetteer(self):
        self.upload(sample.good_zip(), "good.zip")
        self.restore("v2")
        self.assertEqual(self.gazetteer(), sorted(sample.V2_ROWS))

        # 再上传断链的包（v3→v4 增量缺失），恢复 v4 必失败；
        # 原名录与历史恢复记录都要原样保留
        self.upload(sample.brokenv4_zip(), "brokenv4.zip")
        r = self.restore("v4")
        self.assertIn("恢复 v4 失败".encode(), r.data)
        self.assertIn("缺少增量文件".encode(), r.data)
        self.assertEqual(self.gazetteer(), sorted(sample.V2_ROWS))
        self.assertEqual(self.meta("current_version"), "v2")
        statuses = [row["status"] for row in
                    self.query("SELECT * FROM recoveries ORDER BY id")]
        self.assertEqual(statuses, ["success", "failed"])

        # 页面同时展示名录与恢复记录
        r = self.client.get("/")
        self.assertIn("李家屯".encode(), r.data)
        self.assertIn("恢复记录".encode(), r.data)

    def test_search_by_id(self):
        self.upload(sample.good_zip())
        self.restore("v3")
        r = self.client.get("/?q=P0012")
        self.assertIn("孙家沟".encode(), r.data)
        self.assertIn("精确命中".encode(), r.data)
        r = self.client.get("/?q=P9999")
        self.assertIn("没有匹配记录".encode(), r.data)

    def test_restore_unknown_version_explains(self):
        self.upload(sample.good_zip())
        r = self.restore("v9")
        self.assertIn("恢复 v9 失败".encode(), r.data)
        self.assertEqual(self.gazetteer(), [])

    # ---------------- 同一版两份备份、清单摘要各不相同 ----------------
    def test_two_lineages_same_named_zip_uses_working_manifest(self):
        # 两份 ZIP 同名，靠上传批次（#1/#2）区分
        r = self.upload(sample.lineage_a_zip(), "backup.zip")
        self.assertIn("已接收备份包".encode(), r.data)
        r = self.upload(sample.lineage_b_zip(), "backup.zip")
        self.assertIn("已接收备份包".encode(), r.data)

        # A 清单先校验通过的 v2 接不上更名包；应自动改用 B 清单的链恢复 v3
        r = self.restore("v3")
        self.assertIn("已恢复到版本 v3".encode(), r.data)
        self.assertEqual(self.gazetteer(), sorted(sample.V3_ROWS))
        self.assertEqual(self.meta("current_version"), "v3")

        rec = self.query("SELECT * FROM recoveries WHERE status = 'success'")
        self.assertEqual(len(rec), 1)
        files = self.query(
            "SELECT * FROM recovery_files WHERE recovery_id = ?"
            " ORDER BY id", (rec[0]["id"],))
        # 每步列出上传批次和包内路径，同名 ZIP 也能区分
        self.assertEqual([f["kind"] for f in files], ["full", "increment"])
        self.assertTrue(all("#2 backup.zip ::" in f["source"] for f in files))
        self.assertIn("full_v2.json", files[0]["source"])
        self.assertIn("inc_v2_v3.json", files[1]["source"])
        self.assertEqual(files[1]["version_span"], "v2→v3")

        # 页面展示两条上传批次与成功记录里的批次号
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("#1", page)
        self.assertIn("#2", page)

    def test_broken_lineage_failure_explains_and_keeps_history(self):
        # 只有坏链（A 清单的 v2→v3 非法）：页面说明原因，原名录不动，留失败记录
        self.upload(sample.lineage_a_zip(), "backup.zip")
        r = self.restore("v3")
        self.assertIn("恢复 v3 失败".encode(), r.data)
        self.assertIn("没有原记录".encode(), r.data)
        self.assertEqual(self.gazetteer(), [])
        self.assertEqual(
            [row["status"] for row in self.query("SELECT * FROM recoveries")],
            ["failed"])

        # 再补一份能恢复 v2 的包：原名录可恢复，失败记录与新成功记录都可查
        self.upload(sample.good_zip(), "good.zip")
        self.restore("v2")
        self.assertEqual(self.gazetteer(), sorted(sample.V2_ROWS))
        self.assertEqual(self.meta("current_version"), "v2")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("恢复记录", page)
        self.assertIn("good.zip", page)
        statuses = [row["status"] for row in
                    self.query("SELECT * FROM recoveries ORDER BY id")]
        self.assertEqual(statuses, ["failed", "success"])


if __name__ == "__main__":
    unittest.main()
