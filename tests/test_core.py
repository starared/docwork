import threading
import time
import unittest

from base import DBTestCase


class TestSecurity(DBTestCase):
    def test_password(self):
        from app import security
        h = security.hash_password("correct horse battery")
        self.assertTrue(security.verify_password("correct horse battery", h))
        self.assertFalse(security.verify_password("wrong", h))

    def test_totp_rfc6238(self):
        from app import security
        # RFC 6238 测试向量（SHA1，密钥 "12345678901234567890"，取 6 位）
        import base64
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(security.totp_now(secret, 59), "287082")
        self.assertEqual(security.totp_now(secret, 1111111109), "081804")
        self.assertTrue(security.verify_totp(secret, "081804", 1111111109 + 25))

    def test_encrypt(self):
        from app import security
        e = security.encrypt_secret("sk-abc")
        self.assertNotIn("sk-abc", e)
        self.assertEqual(security.decrypt_secret(e), "sk-abc")


class TestAccounts(DBTestCase):
    def test_owner_and_tokens(self):
        from app import accounts, db
        from app.util import UserError, now
        accounts.create_owner("admin", "password1234")
        with self.assertRaises(UserError):
            accounts.owner_login("admin", "bad")
        cookie = accounts.owner_login("admin", "password1234")
        sc = accounts.session_scope(cookie)
        self.assertTrue(sc.is_owner)

        # 一次性令牌：兑换后会话可用，不能再次兑换
        plain, tok = accounts.create_token("张三", now() + 3600, one_time=True, perms=["ppt"])
        c1, _ = accounts.redeem(plain)
        s1 = accounts.session_scope(c1)
        self.assertIsNotNone(s1)
        self.assertEqual(s1.perms, ["ppt"])
        self.assertEqual(s1.workspace_id, tok["workspace_id"])
        with self.assertRaises(UserError):
            accounts.redeem(plain)
        self.assertIsNotNone(accounts.session_scope(c1), "一次性令牌兑换后会话仍应有效")

        # 撤销后会话立即失效
        accounts.revoke_token(tok["id"])
        self.assertIsNone(accounts.session_scope(c1))
        ws = db.one("SELECT * FROM workspaces WHERE id=?", (tok["workspace_id"],))
        self.assertIsNotNone(ws["delete_after"])

        # 续发令牌绑定同一工作区，取消删除计划
        plain2, tok2 = accounts.create_token("张三续", now() + 3600, workspace_id=tok["workspace_id"])
        ws = db.one("SELECT * FROM workspaces WHERE id=?", (tok["workspace_id"],))
        self.assertIsNone(ws["delete_after"])
        c2, _ = accounts.redeem(plain2)
        c3, _ = accounts.redeem(plain2)  # 非一次性可多次兑换
        self.assertIsNotNone(accounts.session_scope(c3))

    def test_expired_token(self):
        from app import accounts, db
        from app.util import now
        plain, tok = accounts.create_token("临时", now() + 3600)
        c, _ = accounts.redeem(plain)
        db.run("UPDATE tokens SET expires_at=? WHERE id=?", (now() - 1, tok["id"]))
        self.assertIsNone(accounts.session_scope(c))

    def test_rate_limit(self):
        from app import accounts
        from app.util import UserError
        for _ in range(3):
            accounts.rate_limit("k1", 3)
        with self.assertRaises(UserError):
            accounts.rate_limit("k1", 3)


class TestJobsAndQuota(DBTestCase):
    def setUp(self):
        from app import db
        db.run("DELETE FROM jobs")
        db.run("DELETE FROM reservations")
        db.run("DELETE FROM usage")

    def test_heavy_global_limit(self):
        from app import jobs
        from app.config import get_settings
        limit = get_settings().heavy_limit
        for i in range(limit + 2):
            jobs.enqueue("convert", "w1", {"i": i})
        claimed = [jobs.claim(["convert", "ocr", "preview", "render"], "w") for _ in range(limit + 2)]
        self.assertEqual(sum(1 for c in claimed if c), limit)

    def test_heavy_parent_waiting_child_no_deadlock(self):
        """重负载父任务（如导入）等待自己的预览子任务：并发上限为 1 时也不能死锁。"""
        import os
        import threading
        from app import config, jobs
        from app.pipeline.context import Ctx
        os.environ["DW_HEAVY_GLOBAL"] = "1"
        config.reset_settings()
        try:
            self.assertEqual(config.get_settings().heavy_limit, 1)
            parent = jobs.enqueue("import_file", "w1", {})
            got = jobs.claim(["convert", "preview"], "w")
            self.assertEqual(got["id"], parent["id"])
            box = {}
            th = threading.Thread(target=lambda: box.update(r=Ctx(got).child("preview", "preview", {"sha": "x"}, timeout=30)))
            th.start()
            child = None
            for _ in range(100):
                child = jobs.claim(["convert", "preview"], "w")
                if child:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(child, "父任务等待期间必须让出名额，子任务才能运行")
            self.assertEqual(child["parent_id"], parent["id"])
            # 名额被子任务占用时，新的重负载任务不能插进来（仍然遵守上限 1）
            other = jobs.enqueue("convert", "w1", {})
            self.assertIsNone(jobs.claim(["convert"], "w"))
            jobs.finish(child["id"], {"pages": []})
            th.join(10)
            self.assertFalse(th.is_alive())
            self.assertEqual(box["r"], {"pages": []})
            self.assertEqual(jobs.get(parent["id"])["heavy"], 1, "子任务结束后父任务重新取得名额")
            self.assertIsNone(jobs.claim(["convert"], "w"), "父任务重新占用名额后，其他任务继续排队")
            jobs.finish(parent["id"], {})
            self.assertEqual(jobs.claim(["convert"], "w")["id"], other["id"])
        finally:
            os.environ.pop("DW_HEAVY_GLOBAL", None)
            config.reset_settings()

    def test_expired_token_stops_jobs(self):
        """令牌到期后：排队和运行中的任务被停止并结算配额；worker 不再执行；续发令牌后同一工作区的任务可继续。"""
        from app import accounts, db, jobs, quota
        from app.scheduler import expire_token_jobs
        from app.util import new_id, now
        from app.worker import execute
        _, tok = accounts.create_token("到期", now() + 3600, quota={"gen_count": 5})
        ws = tok["workspace_id"]

        def mk(kind="convert"):
            jid = new_id("j_")
            with db.tx():
                quota.reserve(jid, tok["id"], kind, 0)
                jobs.enqueue(kind, ws, {}, token_id=tok["id"], job_id=jid)
            return jid
        running, queued = mk(), mk()
        self.assertEqual(jobs.claim(["convert"], "w")["id"], running)
        db.run("UPDATE tokens SET expires_at=? WHERE id=?", (now() - 1, tok["id"]))
        self.assertEqual(expire_token_jobs(), 2)
        self.assertEqual(jobs.get(queued)["status"], "cancelled")
        self.assertIn("到期", jobs.get(queued)["message"])
        self.assertEqual(db.one("SELECT status FROM reservations WHERE job_id=?", (queued,))["status"], "settled")
        self.assertEqual(jobs.get(running)["status"], "cancelling", "运行中的任务在检查点停止")
        self.assertTrue(jobs.cancel_requested(running))
        # worker 认领到已到期令牌的任务时不执行
        late = mk()
        j = jobs.claim(["convert"], "w")
        self.assertEqual(j["id"], late)
        ran = []
        execute(j, {"convert": lambda ctx: ran.append(1) or {}})
        self.assertEqual(ran, [])
        self.assertEqual(jobs.get(late)["status"], "cancelled")
        # 续发：同一工作区有了新的有效令牌，旧令牌的任务可以继续
        accounts.create_token("续发", now() + 3600, workspace_id=ws)
        self.assertIsNone(accounts.job_grant_error(tok["id"]))
        # 撤销始终立即停止
        accounts.revoke_token(tok["id"])
        self.assertEqual(accounts.job_grant_error(tok["id"]), "访问令牌已撤销")

    def test_cancel_and_children(self):
        from app import jobs
        p = jobs.enqueue("gen_ppt", "w1", {})
        c = jobs.enqueue("render", "w1", {}, parent_id=p["id"], step="render")
        again = jobs.enqueue("render", "w1", {}, parent_id=p["id"], step="render")
        self.assertEqual(c["id"], again["id"], "子任务按 (parent, step) 幂等")
        got = jobs.claim(["ai"], "w")
        self.assertEqual(got["id"], p["id"])
        jobs.request_cancel(p["id"])
        self.assertEqual(jobs.get(p["id"])["status"], "cancelling")
        self.assertEqual(jobs.get(c["id"])["status"], "cancelled")
        self.assertTrue(jobs.cancel_requested(p["id"]))

    def test_serial_per_work(self):
        from app import jobs
        a = jobs.enqueue("edit", "w1", {}, work_id="wk1")
        b = jobs.enqueue("edit", "w1", {}, work_id="wk1")
        self.assertEqual(jobs.claim(["ai"], "w")["id"], a["id"])
        self.assertIsNone(jobs.claim(["ai"], "w"), "同一作品的修改任务排队")
        jobs.finish(a["id"], {})
        self.assertEqual(jobs.claim(["ai"], "w")["id"], b["id"])

    def test_stale_requeue(self):
        from app import db, jobs
        j = jobs.enqueue("convert", "w1", {})
        jobs.claim(["convert"], "w")
        db.run("UPDATE jobs SET heartbeat_at=0 WHERE id=?", (j["id"],))
        jobs.requeue_stale(10)
        self.assertEqual(jobs.get(j["id"])["status"], "queued")

    def test_quota_concurrent_reserve(self):
        """多个线程同时预占，不会一起通过余额检查。"""
        from app import accounts, db, jobs, quota
        from app.util import UserError, new_id, now
        _, tok = accounts.create_token("配额", now() + 3600, quota={"gen_count": 3, "tokens": 100000})
        ok, bad = [], []

        def worker():
            try:
                with db.tx() as conn:
                    jid = new_id("j_")
                    quota.reserve(jid, tok["id"], "gen_ppt", 20000, conn=conn)
                    jobs.enqueue("gen_ppt", tok["workspace_id"], {}, token_id=tok["id"], job_id=jid, conn=conn)
                ok.append(jid)
            except UserError:
                bad.append(1)
            finally:
                db.close_thread_conn()

        ts = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(ok), 3)
        self.assertEqual(len(bad), 5)
        # 结算：失败任务不计生成次数，按实际 token 结算
        db.run("UPDATE jobs SET status='failed' WHERE id=?", (ok[0],))
        db.insert("usage", {"id": new_id(), "root_job_id": ok[0], "prompt_tokens": 100, "completion_tokens": 50, "created_at": now()})
        quota.settle(ok[0])
        quota.settle(ok[0])  # 幂等
        u = quota.usage_summary(tok["id"])
        self.assertEqual(u["gen"], 2)
        self.assertEqual(u["tokens"], 40000 + 150)


if __name__ == "__main__":
    unittest.main()


class TestHardening(DBTestCase):
    def _req(self, peer, headers=None):
        from starlette.requests import Request
        hs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        return Request({"type": "http", "method": "GET", "path": "/", "headers": hs, "client": (peer, 1234), "query_string": b""})

    def test_client_ip_trust(self):
        from app.web.common import client_ip
        # 直连的外部地址：转发头一律不信
        self.assertEqual(client_ip(self._req("8.8.8.8", {"X-Forwarded-For": "1.1.1.1", "X-Real-IP": "2.2.2.2"})), "8.8.8.8")
        # 经宿主机 Nginx（Docker 网关）：用 X-Real-IP
        self.assertEqual(client_ip(self._req("172.18.0.1", {"X-Real-IP": "203.0.113.5", "X-Forwarded-For": "6.6.6.6"})), "203.0.113.5")
        # 没有 X-Real-IP 时取 X-Forwarded-For 最后一项（最近一层代理追加），不取客户端可控的第一项
        self.assertEqual(client_ip(self._req("127.0.0.1", {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})), "203.0.113.9")
        # 非法值回退为直连地址
        self.assertEqual(client_ip(self._req("172.18.0.1", {"X-Real-IP": "not-an-ip"})), "172.18.0.1")
        # 172.x 中不属于私有网段的公网地址不被当作代理
        self.assertEqual(client_ip(self._req("172.67.1.1", {"X-Real-IP": "9.9.9.9"})), "172.67.1.1")

    def test_sandbox_mode_reported(self):
        import os
        from app import config
        from app.tools import sandbox
        for mode, expect in (("rlimit", "rlimit"),):
            os.environ["DW_SANDBOX"] = mode
            config.reset_settings()
            self.assertEqual(sandbox.effective_mode(), expect)
        if not sandbox.bwrap_available():
            os.environ["DW_SANDBOX"] = "auto"
            config.reset_settings()
            self.assertEqual(sandbox.effective_mode(), "rlimit")
            os.environ["DW_SANDBOX"] = "bwrap"
            config.reset_settings()
            self.assertEqual(sandbox.effective_mode(), "unavailable")
            from app.tools.sandbox import ToolError, run
            with self.assertRaises(ToolError):
                run(["true"], config.get_settings().tmp_dir)
        os.environ["DW_SANDBOX"] = "rlimit"
        config.reset_settings()

    def test_download_size_limited(self):
        """图片下载（含图像生成返回的 URL）流式读取，超过上限立即中止。"""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from app.llm import LLM, LLMError

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                if self.path == "/len":
                    self.send_header("Content-Length", str(50 * 1024 * 1024))
                self.end_headers()
                try:
                    for _ in range(64):
                        self.wfile.write(b"\0" * 1024 * 1024)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            llm = LLM({"id": "t", "params": {}, "workspace_id": "w", "token_id": None}, lambda: None, lambda t: None)
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            with self.assertRaises(LLMError):
                llm.download(base + "/stream", max_bytes=2 * 1024 * 1024)
            with self.assertRaises(LLMError):
                llm.download(base + "/len", max_bytes=2 * 1024 * 1024)
            with self.assertRaises(LLMError):
                llm.download("file:///etc/passwd")
        finally:
            srv.shutdown()
