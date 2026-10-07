"""端到端：模拟模型接口 + 真实 worker + 真实渲染与 LibreOffice。"""
import threading
import time
import unittest
from pathlib import Path

from base import DBTestCase


class TestPipeline(DBTestCase):
    @classmethod
    def setUpClass(cls):
        import os
        # 按 2 核配置（重负载全局并发 1）运行整套流程：导入等“父任务等待子任务”的流程在最紧的并发下也不能死锁
        os.environ["DW_HEAVY_GLOBAL"] = "1"
        super().setUpClass()
        import mock_llm
        from app import accounts, models_cfg, storage
        from app.worker import Worker
        from app.pipeline import research
        research.ALLOW_PRIVATE = True  # 模拟图库的图片在本机
        cls.srv, port = mock_llm.start()
        base = f"http://127.0.0.1:{port}/v1"
        e = models_cfg.save_endpoint({"kind": "openai", "name": "模拟", "base_url": base, "api_key": "test-key"})
        t, i = models_cfg.add_models(e, ["mock", "mock-img"])
        s = models_cfg.save_endpoint({"kind": "stock", "name": "模拟图库", "base_url": f"http://127.0.0.1:{port}", "api_key": "k", "extra": {"provider": "pexels"}})
        models_cfg.set_roles({"planner": t, "writer": t, "fast": t, "vision": t, "image": i, "stock": s})
        cls.eids = (e, t, i, s)
        accounts.create_owner("admin", "password1234")
        cls.ws = accounts.owner_workspace_id()
        cls.worker = Worker(["ai", "render", "convert", "ocr", "preview"], 4)
        cls.threads = [threading.Thread(target=cls.worker.loop, args=(k,), daemon=True) for k in range(8)]
        for th in cls.threads:
            th.start()

    @classmethod
    def tearDownClass(cls):
        cls.worker.stopping.set()
        for th in cls.threads:
            th.join(timeout=30)
        cls.srv.shutdown()
        import os
        from app.pipeline import research
        research.ALLOW_PRIVATE = False
        os.environ.pop("DW_HEAVY_GLOBAL", None)
        super().tearDownClass()

    def run_job(self, kind, params, work_id=None, timeout=240):
        from app import jobs
        j = jobs.enqueue(kind, self.ws, params, work_id=work_id)
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = jobs.get(j["id"])
            if j["status"] in ("done", "failed", "cancelled", "awaiting_input"):
                return j
            time.sleep(0.3)
        self.fail(f"任务超时：{kind} {j['status']} {j['stage']} {j['message']}")

    def upload(self, path: Path, name=None):
        from app import storage
        from app.tools import limits
        info = limits.inspect(path, name or path.name)
        return storage.store_path_as_file(self.ws, path, name or path.name, "upload", meta={"kind": info["kind"]})

    def test_01_ppt_generate_edit_manual(self):
        from app import works
        j = self.run_job("gen_ppt", {"topic": "测试主题", "pages": 12, "image_mode": "auto"})
        self.assertEqual(j["status"], "done", j["error"])
        v = works.get_version(j["result"]["version_id"])
        self.assertEqual(len(v["spec"]["slides"]), 12)
        m = v["manifest"]
        self.assertIn("pptx", m["exports"])
        self.assertIn("pdf", m["exports"])
        self.assertIn("data_xlsx", m["exports"], "含图表时附带数据 XLSX")
        self.assertEqual(len(m["pages"]), 12)
        self.assertTrue(all(p.get("file_id") for p in m["pages"]))
        cards = [s for s in v["spec"]["slides"] if s["layout"] == "cards"]
        self.assertTrue(cards and all(len(c["heading"]) <= 16 for c in cards[0]["content"]["cards"]), "不合格页面应被修复")
        imgs = [s for s in v["spec"]["slides"] if s["layout"] == "image_text"]
        if imgs:
            self.assertTrue(imgs[0]["content"]["image"]["asset"], "配图应被解析")
        wid = j["result"]["work_id"]
        # 对话修改：只改选中页面
        target = v["spec"]["slides"][3]["id"]
        e = self.run_job("edit", {"instruction": "改标题", "scope": {"type": "slides", "ids": [target]}, "base_version_id": v["id"]}, work_id=wid)
        self.assertEqual(e["status"], "done", e["error"])
        v2 = works.get_version(e["result"]["version_id"])
        self.assertEqual(v2["changed"], [target])
        s_old = {s["id"]: s for s in v["spec"]["slides"]}
        for s in v2["spec"]["slides"]:
            if s["id"] != target:
                self.assertEqual(s, s_old[s["id"]], "范围之外的页面规格必须不变")
        self.assertEqual(v2["spec"]["slides"][3]["title"], "修改后的标题")
        # 未变化的页面复用预览缓存
        p1 = {p["id"]: p["preview"] for p in v["manifest"]["pages"]}
        p2 = {p["id"]: p["preview"] for p in v2["manifest"]["pages"]}
        self.assertEqual(sum(1 for k in p1 if p1[k] == p2.get(k)), 11)
        # 基于旧版本的修改被拒绝
        e2 = self.run_job("edit", {"instruction": "再改", "scope": {"type": "all"}, "base_version_id": v["id"]}, work_id=wid)
        self.assertEqual(e2["status"], "failed")
        self.assertIn("新版本", e2["error"])
        # 直接编辑两次：版本不可变，各生成一个版本，第一次的内容可以原样找回
        path = "content.bullets.0.text"
        sid = next(s["id"] for s in v2["spec"]["slides"] if s["layout"] == "bullets")
        m1 = self.run_job("manual_edit", {"ops": [{"op": "set_field", "slide_id": sid, "path": path, "value": "手动一"}], "base_version_id": v2["id"]}, work_id=wid)
        self.assertEqual(m1["status"], "done", m1["error"])
        m2 = self.run_job("manual_edit", {"ops": [{"op": "set_field", "slide_id": sid, "path": path, "value": "手动二"}], "base_version_id": m1["result"]["version_id"]}, work_id=wid)
        self.assertEqual(m2["status"], "done", m2["error"])
        self.assertNotEqual(m1["result"]["version_id"], m2["result"]["version_id"])
        self.assertEqual(len(works.list_versions(wid)), 4)
        vm1 = works.get_version(m1["result"]["version_id"])
        bl = next(s for s in vm1["spec"]["slides"] if s["id"] == sid)
        self.assertEqual(bl["content"]["bullets"][0]["text"], "手动一")
        # 恢复
        r = works.restore(wid, v["id"])
        self.assertEqual(r["spec"], v["spec"])
        self.assertEqual(len(works.list_versions(wid)), 5)

    def test_02_outline_confirm(self):
        from app import jobs
        j = self.run_job("gen_ppt", {"topic": "确认大纲", "pages": 6, "confirm_outline": True, "image_mode": "none"})
        self.assertEqual(j["status"], "awaiting_input")
        outline = j["result"]["outline"]
        outline["slides"][3]["title"] = "用户修改过的标题"
        jobs.resume(j["id"], {"outline": outline})
        t0 = time.time()
        while time.time() - t0 < 180:
            j = jobs.get(j["id"])
            if j["status"] in ("done", "failed"):
                break
            time.sleep(0.3)
        self.assertEqual(j["status"], "done", j["error"])
        from app import works
        v = works.get_version(j["result"]["version_id"])
        self.assertIn("用户修改过的标题", [s["title"] for s in v["spec"]["slides"]])

    def test_03_doc(self):
        from app import works
        j = self.run_job("gen_doc", {"topic": "写一份报告", "image_mode": "none"})
        self.assertEqual(j["status"], "done", j["error"])
        v = works.get_version(j["result"]["version_id"])
        self.assertIn("docx", v["manifest"]["exports"])
        self.assertTrue(v["manifest"]["block_pages"])
        self.assertEqual(v["manifest"]["issues"], [])
        e = self.run_job("edit", {"instruction": "改一段", "scope": {"type": "all"}, "base_version_id": v["id"]}, work_id=j["result"]["work_id"])
        self.assertEqual(e["status"], "done", e["error"])

    def test_04_xls(self):
        from app import works
        j = self.run_job("gen_xls", {"topic": "做个预算表"})
        self.assertEqual(j["status"], "done", j["error"])
        v = works.get_version(j["result"]["version_id"])
        self.assertEqual(v["manifest"]["issues"], [])
        self.assertEqual(v["manifest"]["preview"]["预算"]["rows"][3][1], 4500)
        e = self.run_job("edit", {"instruction": "房租改成3500", "scope": {"type": "range", "range": "预算!B2:B3"}, "base_version_id": v["id"]}, work_id=j["result"]["work_id"])
        self.assertEqual(e["status"], "done", e["error"])
        v2 = works.get_version(e["result"]["version_id"])
        self.assertEqual(v2["manifest"]["preview"]["预算"]["rows"][3][1], 5000)
        csv = self.tmp / "sales.csv"
        csv.write_text("地区,销售额\n华东,100\n华南,80\n华东,50\n华北,30\n", encoding="utf-8")
        f = self.upload(csv)
        j = self.run_job("gen_xls", {"topic": "按地区汇总", "file_ids": [f["id"]]})
        self.assertEqual(j["status"], "done", j["error"])
        v = works.get_version(j["result"]["version_id"])
        self.assertEqual(v["manifest"]["issues"], [], v["manifest"]["issues"])
        pv = v["manifest"]["preview"]["按地区汇总"]["rows"]
        self.assertEqual(pv[1], ["华东", 150])
        self.assertEqual(pv[-1], ["合计", 260])

    def test_05_import_inplace_and_rebuild(self):
        from app import works
        from app.render.pptx_render import render_deck
        from app.spec.deck import Deck
        from samples import sample_deck
        p = self.tmp / "上传的演示.pptx"
        render_deck(Deck.model_validate(sample_deck()), p, {}, self.tmp / "r")
        f = self.upload(p)
        j = self.run_job("import_file", {"file_id": f["id"]})
        self.assertEqual(j["status"], "done", j["error"])
        wid = j["result"]["work_id"]
        v = works.get_version(j["result"]["version_id"])
        self.assertEqual(len(v["manifest"]["pages"]), 23)
        self.assertTrue(v["manifest"]["report"]["editable"])
        e = self.run_job("import_edit", {"instruction": "改一处", "scope": {"type": "all"}, "base_version_id": v["id"]}, work_id=wid)
        self.assertEqual(e["status"], "done", e["error"])
        self.assertGreaterEqual(e["result"]["applied"], 1)
        v2 = works.get_version(e["result"]["version_id"])
        # 只有被修改的页面预览变化（LibreOffice 偶有个别像素级差异，允许少量误差）
        self.assertGreaterEqual(len(v2["changed"]), e["result"]["applied"])
        self.assertLessEqual(len(v2["changed"]), e["result"]["applied"] + 2)
        # Word、Excel 原位修改：按每页文字判断已修改页，只改了第一段，不应把所有页面都标为已修改
        from app.render.docx_render import render_document
        from app.render.xlsx_render import render_workbook
        from app.spec.document import Document
        from app.spec.workbook import Workbook
        from samples import sample_document, sample_workbook
        d = self.tmp / "原位报告.docx"
        render_document(Document.model_validate(sample_document()), d, {}, self.tmp / "r3")
        x = self.tmp / "原位表格.xlsx"
        render_workbook(Workbook.model_validate(sample_workbook()), x)
        for path in (d, x):
            j = self.run_job("import_file", {"file_id": self.upload(path)["id"]})
            self.assertEqual(j["status"], "done", j["error"])
            v = works.get_version(j["result"]["version_id"])
            e = self.run_job("import_edit", {"instruction": "改一处", "scope": {"type": "all"}, "base_version_id": v["id"]},
                             work_id=j["result"]["work_id"])
            self.assertEqual(e["status"], "done", e["error"])
            v2 = works.get_version(e["result"]["version_id"])
            n = len(v2["manifest"]["pages"])
            self.assertIn("1", v2["changed"], path.name)
            self.assertTrue(n == 1 or len(v2["changed"]) < n, (path.name, v2["changed"], n))
        # PDF 重建为 PPT
        from app.tools import office
        pdf = office.to_pdf(p, self.tmp / "pdf")
        fp = self.upload(pdf)
        r = self.run_job("rebuild", {"file_id": fp["id"], "target": "ppt", "pages": 8, "image_mode": "none"})
        self.assertEqual(r["status"], "done", r["error"])
        self.assertIn("coverage", r["result"])

    def test_06_tools(self):
        from app import models_cfg, storage
        from app.tools import office
        from app.render.docx_render import render_document
        from app.spec.document import Document
        from samples import sample_document
        d = self.tmp / "报告.docx"
        render_document(Document.model_validate(sample_document()), d, {}, self.tmp / "r2")
        f = self.upload(d)
        j = self.run_job("convert", {"file_id": f["id"], "target": "pdf"})
        self.assertEqual(j["status"], "done", j["error"])
        pdf_id = j["result"]["files"][0]["file_id"]
        pdf_rec = storage.get_file(pdf_id)
        self.assertIsNotNone(pdf_rec["expires_at"])
        pf = storage.get_file(pdf_id)
        pf_up = self.upload(storage.blob_path(pf["sha"]), "报告.pdf")
        j = self.run_job("pdf_tool", {"op": "merge", "file_ids": [pf_up["id"], pf_up["id"]]})
        self.assertEqual(j["status"], "done", j["error"])
        j = self.run_job("pdf_tool", {"op": "compress", "file_ids": [pf_up["id"]], "options": {"level": "screen"}})
        self.assertEqual(j["status"], "done", j["error"])
        j = self.run_job("ocr", {"file_id": pf_up["id"], "output": "txt"})
        self.assertEqual(j["status"], "done", j["error"])
        j = self.run_job("model_test", {"endpoint_id": self.eids[0], "what": "list"})
        self.assertEqual(j["status"], "done", j["error"])
        self.assertIn("mock-img", models_cfg.get_endpoint(self.eids[0])["available"])
        j = self.run_job("model_test", {"model_id": self.eids[1], "what": "chat"})
        self.assertEqual(j["status"], "done", j["error"])
        self.assertTrue(j["result"]["ok"] and j["result"]["json"] and j["result"]["vision"])
        j = self.run_job("model_test", {"model_id": self.eids[2], "what": "image"})
        self.assertEqual(j["status"], "done", j["error"])
        self.assertTrue(j["result"]["image"])
        j = self.run_job("model_test", {"endpoint_id": self.eids[3]})
        self.assertEqual(j["status"], "done", j["error"])
        self.assertTrue(j["result"]["ok"])
        j = self.run_job("compat_pack", {})
        self.assertEqual(j["status"], "done", j["error"])

    def test_07_cancel(self):
        from app import jobs
        j = jobs.enqueue("gen_ppt", self.ws, {"topic": "取消", "pages": 20, "image_mode": "none"})
        time.sleep(1.5)
        jobs.request_cancel(j["id"])
        t0 = time.time()
        while time.time() - t0 < 120:
            s = jobs.get(j["id"])["status"]
            if s in ("cancelled", "done", "failed"):
                break
            time.sleep(0.3)
        self.assertEqual(s, "cancelled")
        from app import db
        self.assertIsNone(db.one("SELECT id FROM versions WHERE job_id=?", (j["id"],)), "取消后不写入版本")


if __name__ == "__main__":
    unittest.main()
