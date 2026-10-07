"""联网资料：网页正文提取、网址校验、内网地址拦截、SearXNG 检索接入生成流程。"""
import os
import threading
import time
import unittest

from base import DBTestCase


class TestResearchUnit(unittest.TestCase):
    def test_html_text(self):
        from app.pipeline import research
        html = ("<html><head><title> 标题 </title><style>p{}</style></head><body><header>页眉</header>"
                "<div>侧边</div><main><p>第一段 <b>粗体</b></p><ul><li>一</li><li>二</li></ul></main>"
                "<footer>页脚</footer></body></html>").encode()
        title, text = research.html_text(html)
        self.assertEqual(title, "标题")
        self.assertIn("第一段 粗体", text)
        self.assertIn("一\n二", text)
        for junk in ("页眉", "页脚", "p{}", "侧边"):
            self.assertNotIn(junk, text)
        # GBK 网页按响应头声明的编码解码
        self.assertIn("中文", research.html_text("<p>中文内容</p>".encode("gbk"), "gbk")[1])
        # 没有声明编码的 GBK 网页
        self.assertIn("中文", research.html_text("<p>中文内容</p>".encode("gbk"))[1])
        self.assertEqual(research.html_text(b""), ("", ""))

    def test_clean_urls(self):
        from app.pipeline import research
        from app.util import UserError
        self.assertEqual(research.clean_urls("https://a.com/x\nhttps://a.com/x  http://b.org"), ["https://a.com/x", "http://b.org"])
        for bad in (["file:///etc/passwd"], ["javascript:alert(1)"], ["https://"], [f"https://a.com/{i}" for i in range(6)]):
            with self.assertRaises(UserError):
                research.clean_urls(bad)

    def test_private_blocked(self):
        from app.pipeline import research
        from app.util import UserError
        self.assertFalse(research.ALLOW_PRIVATE)
        for u in ("http://127.0.0.1:1/", "http://localhost/", "http://10.0.0.1/", "http://169.254.169.254/latest/meta-data/",
                  "http://[::1]/", "http://192.168.1.1/"):
            with self.assertRaises(UserError, msg=u):
                research.fetch(u)


class TestResearchPipeline(DBTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import mock_llm
        from app import accounts, config, models_cfg
        from app.pipeline import research
        from app.worker import Worker
        cls.srv, port = mock_llm.start()
        cls.base = f"http://127.0.0.1:{port}"
        os.environ["DW_SEARXNG_URL"] = cls.base
        config.reset_settings()
        research.ALLOW_PRIVATE = True
        e = models_cfg.save_endpoint({"kind": "openai", "name": "模拟", "base_url": cls.base + "/v1", "api_key": "test-key"})
        t, _ = models_cfg.add_models(e, ["mock", "mock-img"])
        models_cfg.set_roles({"planner": t, "writer": t, "fast": t, "vision": t})
        accounts.create_owner("admin", "password1234")
        cls.ws = accounts.owner_workspace_id()
        cls.worker = Worker(["ai", "render", "convert", "ocr", "preview"], 4)
        cls.threads = [threading.Thread(target=cls.worker.loop, args=(k,), daemon=True) for k in range(6)]
        for th in cls.threads:
            th.start()

    @classmethod
    def tearDownClass(cls):
        from app import config
        from app.pipeline import research
        cls.worker.stopping.set()
        for th in cls.threads:
            th.join(timeout=30)
        cls.srv.shutdown()
        research.ALLOW_PRIVATE = False
        os.environ.pop("DW_SEARXNG_URL", None)
        config.reset_settings()
        super().tearDownClass()

    def run_job(self, kind, params, timeout=240):
        from app import jobs
        j = jobs.enqueue(kind, self.ws, params)
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = jobs.get(j["id"])
            if j["status"] in ("done", "failed", "cancelled", "awaiting_input"):
                return j
            time.sleep(0.3)
        self.fail(f"任务超时：{kind} {j['status']}")

    def test_web_sources(self):
        from app.pipeline import research
        from app.pipeline.context import Ctx
        from app.llm import LLM
        job = {"id": "j_test", "params": {"topic": "市场", "web_search": True, "urls": [self.base + "/redirect", self.base + "/nope"]},
               "workspace_id": self.ws}

        class FakeCtx:
            p = job["params"]
            llm = LLM(job)
            def progress(self, *a, **k): pass
            def check(self): pass
        text, refs, notes = research.web_sources(FakeCtx(), 0, 1)
        # 用户网址经重定向读到正文、排在最前；两个检索词的相同结果只读一次；读不到的检索结果退回摘要；非 http 结果被丢弃
        self.assertEqual([r["url"] for r in refs], [self.base + "/redirect", self.base + "/page/a", self.base + "/page/missing"])
        self.assertEqual([r["n"] for r in refs], [1, 2, 3])
        self.assertIn("3.2 万亿元", text)
        self.assertNotIn("版权所有", text)
        self.assertIn("（只有检索摘要）乙的摘要", text)
        self.assertTrue(any("/nope" in n for n in notes), notes)

    def test_gen_doc_with_web(self):
        from app import works
        j = self.run_job("gen_doc", {"topic": "写一份市场报告", "image_mode": "none", "web_search": True})
        self.assertEqual(j["status"], "done", j["error"])
        v = works.get_version(j["result"]["version_id"])
        refs = [b for b in v["spec"]["blocks"] if b["type"] == "references"]
        self.assertEqual(len(refs), 1)
        self.assertTrue(refs[0]["items"][0].startswith("网页甲[EB/OL]."), refs[0]["items"])
        self.assertTrue(refs[0]["items"][0].endswith(self.base + "/page/a."))
        # 模型重复写的网络资料被去掉，其他文献接在网络资料后面
        self.assertEqual(sum("/page/a" in it for it in refs[0]["items"]), 1)
        self.assertEqual(refs[0]["items"][-1], "张三. 某本书[M]. 北京: 出版社, 2020.")

    def test_image_via_chat_fallback(self):
        """生图接口不可用、只能通过对话接口出图的模型：自动改走对话接口，并记住以后直接走对话接口。"""
        from app import models_cfg
        from app.llm import test_model
        e = models_cfg.list_endpoints()[0]["id"]
        mid = models_cfg.add_models(e, ["mock-chatimg"])[0]
        caps = test_model(mid, "image")
        self.assertTrue(caps["image"], caps)
        self.assertEqual(caps.get("image_api"), "chat")
        # 记住之后再次调用直接走对话接口
        from app.llm import LLM
        data = LLM({"id": None}, override=models_cfg.get_model(mid)).generate_image("test", size="1792x1024")
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_gen_ppt_with_web(self):
        from app import works
        j = self.run_job("gen_ppt", {"topic": "市场概况", "pages": 5, "image_mode": "none", "urls": [self.base + "/page/a"]})
        self.assertEqual(j["status"], "done", j["error"])
        v = works.get_version(j["result"]["version_id"])
        self.assertIn(self.base + "/page/a", v["spec"]["slides"][-1]["notes"])


if __name__ == "__main__":
    unittest.main()
