"""Web 接口：登录、CSRF、令牌、隔离与越权、上传下载、配额、分享、后台。"""
import time
import unittest

from base import DBTestCase


class TestAPI(DBTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from starlette.testclient import TestClient
        from app import accounts
        from app.web.app import build
        accounts.create_owner("admin", "password1234")
        cls.app = build()
        cls.TestClient = TestClient

    def client(self):
        return self.TestClient(self.app)

    def owner(self):
        from app import db
        db.run("DELETE FROM rate")  # 登录限流另有单独测试；这里每个用例都要登录，避免累计触发
        c = self.client()
        r = c.post("/api/login", json={"username": "admin", "password": "password1234"})
        self.assertEqual(r.status_code, 200, r.text)
        c.headers["X-CSRF-Token"] = c.get("/api/me").json()["csrf"]
        return c

    def guest(self, owner, **kw):
        body = {"note": kw.pop("note", "访客"), "hours": 2, **kw}
        r = owner.post("/api/admin/tokens", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        tok = r.json()
        c = self.client()
        r = c.post("/api/redeem", json={"token": tok["token"]})
        self.assertEqual(r.status_code, 200, r.text)
        c.headers["X-CSRF-Token"] = c.get("/api/me").json()["csrf"]
        return c, tok

    def upload(self, c, name: str, data: bytes, purpose="source"):
        r = c.post("/api/uploads", json={"name": name, "size": len(data)})
        self.assertEqual(r.status_code, 200, r.text)
        u = r.json()
        cs = u["chunk_size"]
        for i in range(u["chunks"]):
            r = c.put(f"/api/uploads/{u['upload_id']}/{i}", content=data[i * cs:(i + 1) * cs])
            self.assertEqual(r.status_code, 200, r.text)
        r = c.post(f"/api/uploads/{u['upload_id']}/complete", json={"purpose": purpose})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_login_csrf(self):
        c = self.client()
        self.assertFalse(c.get("/api/me").json()["authenticated"])
        self.assertEqual(c.get("/api/works").status_code, 401)
        r = c.post("/api/login", json={"username": "admin", "password": "wrong"})
        self.assertEqual(r.status_code, 401)
        c.post("/api/login", json={"username": "admin", "password": "password1234"})
        me = c.get("/api/me").json()
        self.assertEqual(me["kind"], "owner")
        # 缺少 CSRF 头的写操作被拒绝
        r = c.post("/api/admin/tokens", json={"note": "x"})
        self.assertEqual(r.status_code, 403)
        # 非 JSON 请求体被拒绝
        c.headers["X-CSRF-Token"] = me["csrf"]
        r = c.post("/api/admin/tokens", content=b"note=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status_code, 415)
        r = c.get("/api/me")
        self.assertIn("Content-Security-Policy", r.headers)
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")

    def test_tokens_and_isolation(self):
        o = self.owner()
        a, ta = self.guest(o, note="甲", perms=["ppt", "convert", "pdf", "import", "edit"])
        b, tb = self.guest(o, note="乙")
        # 甲上传文件、创建作品
        f = self.upload(a, "a.md", "# 甲的资料\n\n内容".encode())
        from app import storage, works
        from app.accounts import session_scope
        ws_a = a.get("/api/me").json()["workspace_id"]
        wid = works.create_work(ws_a, "ppt", "甲的作品")
        # 乙看不到甲的任何东西
        self.assertEqual(b.get(f"/api/files/{f['id']}").status_code, 404)
        self.assertEqual(b.get(f"/api/files/{f['id']}/download").status_code, 404)
        self.assertEqual(b.get(f"/api/works/{wid}").status_code, 404)
        self.assertEqual(b.get("/api/works").json()["total"], 0)
        self.assertEqual(b.patch(f"/api/works/{wid}", json={"title": "改"}).status_code, 404)
        self.assertEqual(b.delete(f"/api/works/{wid}").status_code, 404)
        self.assertEqual(b.post("/api/jobs", json={"kind": "convert", "params": {"file_id": f["id"], "target": "docx"}}).status_code, 404)
        self.assertEqual(b.post("/api/jobs", json={"kind": "edit", "work_id": wid, "params": {"instruction": "x"}}).status_code, 404)
        # 访客不能访问后台
        for path in ("/api/admin/tokens", "/api/admin/system", "/api/admin/endpoints", "/api/admin/usage", "/api/admin/audit"):
            self.assertEqual(b.get(path).status_code, 403, path)
        # 权限：甲没有 doc 权限
        r = a.post("/api/jobs", json={"kind": "gen_doc", "params": {"topic": "x"}})
        self.assertEqual(r.status_code, 403)
        # 甲可以看自己的
        self.assertEqual(a.get(f"/api/works/{wid}").status_code, 200)
        self.assertEqual(a.get("/api/works").json()["total"], 1)
        # 主人分享只读给乙
        r = o.post(f"/api/works/{wid}/share", json={"workspace_id": b.get("/api/me").json()["workspace_id"], "mode": "readonly"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(b.get(f"/api/works/{wid}").status_code, 200)
        self.assertEqual(b.patch(f"/api/works/{wid}", json={"title": "改"}).status_code, 404, "只读分享不能修改")
        # 撤销后甲的会话立即失效
        r = o.post(f"/api/admin/tokens/{ta['item']['id']}/revoke")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(a.get("/api/works").status_code, 401)
        self.assertEqual(a.get(f"/api/files/{f['id']}/download").status_code, 401, "撤销后下载地址也立即失效")

    def test_one_time_and_retention(self):
        o = self.owner()
        r = o.post("/api/admin/tokens", json={"note": "一次性", "hours": 1, "one_time": True}).json()
        c1 = self.client()
        self.assertEqual(c1.post("/api/redeem", json={"token": r["token"]}).status_code, 200)
        c2 = self.client()
        self.assertEqual(c2.post("/api/redeem", json={"token": r["token"]}).status_code, 401)
        self.assertEqual(c1.get("/api/me").json()["kind"], "guest", "已兑换的会话仍可用")
        items = {t["id"]: t for t in o.get("/api/admin/tokens").json()["items"]}
        t = items[r["item"]["id"]]
        self.assertEqual((t["grant"], t["redeem"]), ("active", "redeemed"))
        # 续发令牌绑定同一工作区
        o.post(f"/api/admin/tokens/{t['id']}/revoke")
        ws = next(w for w in o.get("/api/admin/workspaces").json()["items"] if w["id"] == t["workspace_id"])
        self.assertIsNotNone(ws["delete_after"])
        r2 = o.post("/api/admin/tokens", json={"note": "续", "hours": 1, "workspace_id": t["workspace_id"]})
        self.assertEqual(r2.status_code, 200, r2.text)
        ws = next(w for w in o.get("/api/admin/workspaces").json()["items"] if w["id"] == t["workspace_id"])
        self.assertIsNone(ws["delete_after"])

    def test_upload_resume_and_limits(self):
        o = self.owner()
        data = b"x" * (5 * 1024 * 1024 + 100)
        r = o.post("/api/uploads", json={"name": "big.txt", "size": len(data)}).json()
        uid = r["upload_id"]
        self.assertEqual(r["chunks"], 2)
        o.put(f"/api/uploads/{uid}/1", content=data[5 * 1024 * 1024:])
        st = o.get(f"/api/uploads/{uid}").json()
        self.assertEqual(st["received"], [1])
        self.assertEqual(o.post(f"/api/uploads/{uid}/complete", json={}).status_code, 400)
        o.put(f"/api/uploads/{uid}/0", content=data[:5 * 1024 * 1024])
        r = o.post(f"/api/uploads/{uid}/complete", json={})
        self.assertEqual(r.status_code, 200, r.text)
        # 伪装的文件被拒绝
        r = o.post("/api/uploads", json={"name": "fake.pdf", "size": 10}).json()
        o.put(f"/api/uploads/{r['upload_id']}/0", content=b"MZ12345678")
        self.assertEqual(o.post(f"/api/uploads/{r['upload_id']}/complete", json={}).status_code, 415)
        # 分片大小不符
        r = o.post("/api/uploads", json={"name": "a.txt", "size": 10}).json()
        self.assertEqual(o.put(f"/api/uploads/{r['upload_id']}/0", content=b"123").status_code, 400)

    def test_quota_via_api(self):
        from app import models_cfg
        o = self.owner()
        eid = models_cfg.save_endpoint({"kind": "openai", "base_url": "http://127.0.0.1:9/v1", "api_key": "k"})
        models_cfg.set_roles({"planner": models_cfg.add_models(eid, ["m"])[0]})
        g, t = self.guest(o, quota={"gen_count": 1, "concurrent": 5, "upload_mb": 1})
        r = g.post("/api/jobs", json={"kind": "gen_ppt", "params": {"topic": "一", "pages": 5}})
        self.assertEqual(r.status_code, 200, r.text)
        r = g.post("/api/jobs", json={"kind": "gen_ppt", "params": {"topic": "二", "pages": 5}})
        self.assertEqual(r.status_code, 429)
        self.assertIn("生成次数", r.json()["error"])
        r = g.post("/api/uploads", json={"name": "a.txt", "size": 2 * 1024 * 1024})
        self.assertEqual(r.status_code, 413)
        # 取消排队中的任务释放配额
        jid = g.get("/api/jobs").json()["items"][0]["id"]
        self.assertEqual(g.post(f"/api/jobs/{jid}/cancel").status_code, 200)
        r = g.post("/api/jobs", json={"kind": "gen_ppt", "params": {"topic": "三", "pages": 5}})
        self.assertEqual(r.status_code, 200, r.text)

    def test_download_and_versions(self):
        from app import storage, works
        o = self.owner()
        me = o.get("/api/me").json()
        p = self.tmp / "x.txt"
        p.write_text("hello")
        sha, size = storage.put_file(p)
        wid = works.create_work(me["workspace_id"], "ppt", "测试")
        v = works.add_version(wid, job_id=None, source="generate", spec={"a": 1},
                              manifest={"title": "测试", "exports": {"pptx": {"sha": sha, "size": size, "name": "测试.pptx"}}, "pages": []})
        d = o.get(f"/api/works/{wid}").json()
        fid = d["version"]["manifest"]["exports"]["pptx"]["file_id"]
        r = o.get(f"/api/files/{fid}/download")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"hello")
        self.assertIn("filename*=UTF-8''%E6%B5%8B%E8%AF%95.pptx", r.headers["content-disposition"])
        works.add_version(wid, job_id=None, source="chat", spec={"a": 2}, manifest={"title": "测试"})
        vs = o.get(f"/api/works/{wid}/versions").json()["items"]
        self.assertEqual(len(vs), 2)
        r = o.post(f"/api/works/{wid}/restore", json={"version_id": v["id"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(o.get(f"/api/works/{wid}").json()["version"]["spec"], {"a": 1})
        self.assertEqual(o.post(f"/api/versions/{vs[0]['id']}/star", json={"starred": True}).status_code, 200)
        # 回收站
        self.assertEqual(o.delete(f"/api/works/{wid}").status_code, 200)
        self.assertEqual(o.get("/api/works").json()["total"], 0)
        self.assertEqual(o.get("/api/works?trash=1").json()["total"], 1)
        self.assertEqual(o.post(f"/api/works/{wid}/untrash").status_code, 200)
        # 搜索（trigram）
        works.index(wid, "测试", "关于荧光探针的研究报告")
        self.assertEqual(o.get("/api/works?q=荧光探针").json()["total"], 1)
        self.assertEqual(o.get("/api/works?q=不存在的词").json()["total"], 0)
        # 两个字的词：只出现在正文中也要能搜到
        self.assertEqual(o.get("/api/works?q=荧光").json()["total"], 1)
        self.assertEqual(o.get("/api/works?q=无关").json()["total"], 0)

    def test_job_cannot_target_foreign_work(self):
        """有生成权限的访客即使知道别人作品的 ID，也不能让生成、转换等任务写进那个作品。"""
        from unittest import mock
        from app import jobs, works
        from app.web import api_jobs
        o = self.owner()
        a, _ = self.guest(o, note="越权", perms=["ppt", "convert", "import", "edit"])
        wid = works.create_work(o.get("/api/me").json()["workspace_id"], "ppt", "主人的作品")
        f = self.upload(a, "a.md", "# 资料".encode())
        with mock.patch.object(api_jobs.models_cfg, "status", return_value={"text": True}):
            r = a.post("/api/jobs", json={"kind": "gen_ppt", "work_id": wid, "params": {"topic": "x"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["work_id"])
        self.assertIsNone(jobs.get(r.json()["id"])["work_id"])
        r = a.post("/api/jobs", json={"kind": "convert", "work_id": wid, "params": {"file_id": f["id"], "target": "docx"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["work_id"])
        # 修改类任务仍然检查写权限
        self.assertEqual(a.post("/api/jobs", json={"kind": "manual_edit", "work_id": wid, "params": {"ops": []}}).status_code, 404)
        # 纵深防御：即使任务里带了别人的作品 ID，生成流程也会新建作品
        from app.pipeline.common import ensure_work
        from app.pipeline.context import Ctx
        j = jobs.enqueue("gen_ppt", a.get("/api/me").json()["workspace_id"], {}, work_id=wid)
        self.assertNotEqual(ensure_work(Ctx(jobs.get(j["id"])), "ppt", "新"), wid)
        self.assertIsNone(works.current_version(wid))

    def test_storage_quota_counts_pending_uploads(self):
        """1 MB 存储配额：先同时开始两个 0.7 MB 的上传，第二个必须被拒绝；超时未完成的上传不再占用配额。"""
        from app import db
        from app.scheduler import clean_tmp
        o = self.owner()
        g, _ = self.guest(o, note="存储", quota={"storage_mb": 1})
        size = 700 * 1024
        r1 = g.post("/api/uploads", json={"name": "a.txt", "size": size})
        self.assertEqual(r1.status_code, 200, r1.text)
        r2 = g.post("/api/uploads", json={"name": "b.txt", "size": size})
        self.assertEqual(r2.status_code, 413, "未完成的上传也计入配额")
        # 完成第一个后，第二个依然超额
        u = r1.json()
        self.assertEqual(g.put(f"/api/uploads/{u['upload_id']}/0", content=b"a" * size).status_code, 200)
        self.assertEqual(g.post(f"/api/uploads/{u['upload_id']}/complete", json={}).status_code, 200)
        self.assertEqual(g.post("/api/uploads", json={"name": "b.txt", "size": size}).status_code, 413)
        # 被放弃的上传超时后释放配额
        g.post("/api/uploads", json={"name": "c.txt", "size": 200 * 1024})
        db.run("UPDATE uploads SET created_at=0")
        clean_tmp()
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM uploads")["n"], 0)
        self.assertEqual(g.post("/api/uploads", json={"name": "d.txt", "size": 300 * 1024}).status_code, 200)

    def test_token_update_extends_sessions_and_validates(self):
        """延长令牌有效期后，已登录的访客会话同步延长；修改参数与创建时使用同一套校验。"""
        from app import accounts, db
        from app.util import now
        o = self.owner()
        g, tok = self.guest(o, note="续期", hours=1, one_time=True)
        tid = tok["item"]["id"]
        new_exp = now() + 5 * 3600
        r = o.patch(f"/api/admin/tokens/{tid}", json={"expires_at": new_exp})
        self.assertEqual(r.status_code, 200, r.text)
        sess = db.all_("SELECT expires_at FROM sessions WHERE token_id=?", (tid,))
        self.assertTrue(sess and all(abs(x["expires_at"] - new_exp) < 1 for x in sess), "会话跟随令牌延期")
        # 模拟旧到期时间已过：会话仍然有效（一次性令牌无法重新兑换，所以必须靠会话延期）
        cookie = g.cookies.get("dw_session")
        from unittest import mock
        with mock.patch("app.accounts.now", return_value=now() + 2 * 3600):
            self.assertIsNotNone(accounts.session_scope(cookie))
        # 校验：非法值返回 400，而不是 500；未知配额字段、负数被拒绝
        for bad in ({"quota": {"gen_count": "abc"}}, {"quota": {"gen_count": -1}}, {"quota": {"hack": 1}},
                    {"quota": "x"}, {"expires_at": "tomorrow"}, {"expires_at": now() - 10}, {"perms": "all"}):
            r = o.patch(f"/api/admin/tokens/{tid}", json=bad)
            self.assertEqual(r.status_code, 400, (bad, r.text))
        for bad in ({"quota": {"storage_mb": "1.5"}}, {"quota": {"x": 1}}, {"hours": "abc"}):
            r = o.post("/api/admin/tokens", json={"note": "x", **bad})
            self.assertEqual(r.status_code, 400, (bad, r.text))
        self.assertEqual(o.patch(f"/api/admin/tokens/{tid}", json={"quota": {"gen_count": "3"}}).json()["quota"], {"gen_count": 3})

    def test_upload_complete_counts_other_pending(self):
        """上传完成时也计入其他尚未完成的上传：期间工作区新增了文件，两个已开始的上传不能一起越过配额。"""
        from app import storage
        o = self.owner()
        g, _ = self.guest(o, note="完成检查", quota={"storage_mb": 1})
        ws = g.get("/api/me").json()["workspace_id"]
        self.upload(g, "base.txt", b"x" * 400 * 1024)
        a = g.post("/api/uploads", json={"name": "a.txt", "size": 300 * 1024}).json()
        b = g.post("/api/uploads", json={"name": "b.txt", "size": 300 * 1024})
        self.assertEqual(b.status_code, 200, "开始时总量 1.0 MB 未超额")
        b = b.json()
        # 上传期间工作区新增 200 KB 的任务结果
        p = self.tmp / "out.bin"
        p.write_bytes(b"y" * 200 * 1024)
        storage.store_path_as_file(ws, p, "out.bin", "output")
        self.assertEqual(g.put(f"/api/uploads/{a['upload_id']}/0", content=b"a" * 300 * 1024).status_code, 200)
        r = g.post(f"/api/uploads/{a['upload_id']}/complete", json={})
        self.assertEqual(r.status_code, 413, "0.4 + 0.2 + 0.3（另一个待完成）+ 0.3 > 1 MB")
        self.assertEqual(g.put(f"/api/uploads/{b['upload_id']}/0", content=b"b" * 300 * 1024).status_code, 200)
        self.assertEqual(g.post(f"/api/uploads/{b['upload_id']}/complete", json={}).status_code, 200, "第一个被拒绝后，第二个可以完成")

    def test_rate_limit_ignores_spoofed_forwarded_for(self):
        """客户端自己带的 X-Forwarded-For / X-Real-IP 不能绕过登录限流。"""
        from app import db
        db.run("DELETE FROM rate")
        c = self.client()
        codes = []
        for i in range(12):
            r = c.post("/api/login", json={"username": "admin", "password": "wrong-password"},
                       headers={"X-Forwarded-For": f"10.9.{i}.1", "X-Real-IP": f"10.8.{i}.1"})
            codes.append(r.status_code)
        self.assertIn(429, codes, codes)
        db.run("DELETE FROM rate")

    def test_sse_stops_after_revoke(self):
        """访客打开任务进度推送后令牌被撤销：推送立即结束，不再继续发送任务信息。"""
        from app import jobs
        from app.web import api_jobs
        o = self.owner()
        g, tok = self.guest(o, note="推送")
        j = jobs.enqueue("convert", g.get("/api/me").json()["workspace_id"], {}, token_id=tok["item"]["id"])
        old = api_jobs.SSE_AUTH_RECHECK
        api_jobs.SSE_AUTH_RECHECK = 0.3
        try:
            import threading
            from app import accounts
            # 测试客户端会等整个响应结束才返回，所以在另一个线程里于连接建立后撤销令牌
            t = threading.Timer(1.0, accounts.revoke_token, args=(tok["item"]["id"],))
            t.start()
            t0 = time.time()
            r = g.get(f"/api/jobs/{j['id']}/events")
            t.join()
            self.assertEqual(r.status_code, 200)
            events = [ln for ln in r.text.splitlines() if ln.startswith("event:")]
            self.assertEqual(events[0], "event: job", "撤销前正常推送")
            self.assertEqual(events[-1], "event: denied", "撤销后推送结束")
            self.assertLess(time.time() - t0, 10)
        finally:
            api_jobs.SSE_AUTH_RECHECK = old

    def test_zip_entry_names_sanitized(self):
        import io
        import zipfile
        from app import db, storage
        o = self.owner()
        ws = o.get("/api/me").json()["workspace_id"]
        p = self.tmp / "z.txt"
        p.write_text("z")
        f1 = storage.store_path_as_file(ws, p, "../../outside.txt", "output")
        self.assertNotIn("/", f1["name"])
        f2 = storage.store_path_as_file(ws, p, "ok.txt", "output")
        db.run("UPDATE files SET name=? WHERE id=?", ("..\\..\\evil/../x.txt", f2["id"]))  # 模拟旧数据
        r = o.post("/api/files/zip", json={"file_ids": [f1["id"], f2["id"]]})
        self.assertEqual(r.status_code, 200, r.text)
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        self.assertEqual(len(names), 2)
        for n in names:
            self.assertNotIn("/", n)
            self.assertNotIn("\\", n)
            self.assertFalse(n.startswith("."), n)

    def test_trash_hard_delete(self):
        from app import works
        o = self.owner()
        wid = works.create_work(o.get("/api/me").json()["workspace_id"], "doc", "要删除的")
        self.assertEqual(o.delete(f"/api/works/{wid}?hard=1").status_code, 400, "必须先移到回收站")
        self.assertEqual(o.delete(f"/api/works/{wid}").status_code, 200)
        r = o.delete(f"/api/works/{wid}?hard=1")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(o.get(f"/api/works/{wid}").status_code, 404)
        self.assertEqual(o.get("/api/works?trash=1").json()["total"], 0)

    def test_pruned_previews_regenerate(self):
        """历史版本预览被清理后：清单不再引用（空间可回收），查看时自动重新生成，图片可以访问。"""
        from reportlab.pdfgen import canvas
        from app import db, jobs, storage, works
        from app.scheduler import gc_blobs, prune_previews
        from app.worker import execute
        o = self.owner()
        ws = o.get("/api/me").json()["workspace_id"]
        pdf = self.tmp / "v.pdf"
        c = canvas.Canvas(str(pdf))
        for i in range(2):
            c.drawString(100, 700, f"page {i + 1}")
            c.showPage()
        c.save()
        psha, psize = storage.put_file(pdf)
        png = self.tmp / "p.png"
        from PIL import Image
        Image.new("RGB", (40, 30), "red").save(png)
        gsha, gsize = storage.put_file(png)
        pages = [{"id": "s1", "hash": "h1", "preview": gsha, "size": gsize}, {"id": "s2", "hash": "h2", "preview": gsha, "size": gsize}]
        wid = works.create_work(ws, "ppt", "旧预览")
        v1 = works.add_version(wid, job_id=None, source="generate", spec={"a": 1},
                               manifest={"title": "旧预览", "exports": {"pdf": {"sha": psha, "size": psize, "name": "v.pdf"}}, "pages": pages})
        works.add_version(wid, job_id=None, source="chat", spec={"a": 2}, manifest={"title": "旧预览", "pages": []})
        db.run("UPDATE versions SET created_at=0 WHERE id=?", (v1["id"],))
        self.assertEqual(prune_previews(), 1)
        m = works.get_version(v1["id"])["manifest"]
        self.assertTrue(m["previews_pruned"])
        self.assertTrue(all("preview" not in p and "file_id" not in p for p in m["pages"]))
        self.assertIsNone(db.one("SELECT 1 AS x FROM files WHERE version_id=? AND kind='preview'", (v1["id"],)))
        db.run("UPDATE blobs SET created_at=0 WHERE sha=?", (gsha,))
        gc_blobs(0)
        self.assertIsNone(db.one("SELECT 1 AS x FROM blobs WHERE sha=?", (gsha,)), "预览图不再被引用，空间被回收")
        # 查看历史版本：排队重新生成（重复查看不重复排队）
        d = o.get(f"/api/works/{wid}?version={v1['id']}").json()
        st = d["version"]["previews"]
        self.assertEqual(st["state"], "pending")
        self.assertEqual(o.get(f"/api/works/{wid}?version={v1['id']}").json()["version"]["previews"]["job_id"], st["job_id"])
        diff = o.get(f"/api/works/{wid}/diff?a={v1['id']}&b={d['current_version_id']}").json()
        self.assertEqual(diff["previews_pending"], [st["job_id"]])
        # 只读分享的访客也能查看重新生成的进度（不能因为任务不在自己的工作区而一直等待）
        b, _ = self.guest(o, note="只读")
        o.post(f"/api/works/{wid}/share", json={"workspace_id": b.get("/api/me").json()["workspace_id"], "mode": "readonly"})
        bst = b.get(f"/api/works/{wid}?version={v1['id']}").json()["version"]["previews"]
        self.assertEqual(bst["job_id"], st["job_id"])
        self.assertEqual(b.get(f"/api/jobs/{st['job_id']}").status_code, 200)
        self.assertEqual(b.post(f"/api/jobs/{st['job_id']}/cancel").status_code, 404, "只读访客只能查看，不能取消")
        c, _ = self.guest(o, note="无关")
        self.assertEqual(c.get(f"/api/jobs/{st['job_id']}").status_code, 404, "没有作品权限的人仍然看不到")
        j = jobs.claim(["preview"], "w")
        self.assertEqual(j["id"], st["job_id"])
        execute(j)
        self.assertEqual(jobs.get(j["id"])["status"], "done", jobs.get(j["id"])["error"])
        d = o.get(f"/api/works/{wid}?version={v1['id']}").json()
        self.assertEqual(d["version"]["previews"]["state"], "ok")
        pg = d["version"]["manifest"]["pages"]
        self.assertEqual([p["id"] for p in pg], ["s1", "s2"], "页面 ID 不变，版本对比照常对齐")
        r = o.get(f"/api/files/{pg[0]['file_id']}/download?inline=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b.get(f"/api/jobs/{st['job_id']}").json()["status"], "done")
        bpg = b.get(f"/api/works/{wid}?version={v1['id']}").json()["version"]["manifest"]["pages"]
        self.assertEqual(b.get(f"/api/files/{bpg[0]['file_id']}/download?inline=1").status_code, 200, "只读访客能看到重新生成的预览")

    def test_admin_settings_and_endpoints(self):
        o = self.owner()
        r = o.put("/api/admin/settings", json={"keep_versions": 10})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["settings"]["keep_versions"], 10)
        self.assertEqual(o.put("/api/admin/settings", json={"keep_versions": 0}).status_code, 400)
        r = o.post("/api/admin/endpoints", json={"kind": "openai", "name": "t", "base_url": "https://api.example.com/v1", "api_key": "sk-secret"})
        eid = r.json()["id"]
        r = o.post(f"/api/admin/endpoints/{eid}/models", json={"models": ["m-chat", "m-vl", "m-chat"]})
        self.assertEqual(r.status_code, 200, r.text)
        chat, vl, dup = r.json()["ids"]
        self.assertEqual(chat, dup, "重复添加同名模型只保留一个")
        lst = o.get("/api/admin/endpoints").json()
        self.assertNotIn("sk-secret", str(lst), "API Key 不会返回前端")
        self.assertTrue(any(e["id"] == eid and e["has_key"] for e in lst["items"]))
        self.assertEqual({m["model"] for m in lst["models"]}, {"m-chat", "m-vl"})
        # 模型不分类：同一个模型可以同时负责文字、视觉、生图
        r = o.put("/api/admin/roles", json={"planner": vl, "writer": chat, "vision": vl, "image": vl})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["roles"]["vision"], vl)
        self.assertEqual(o.put("/api/admin/roles", json={"planner": eid}).status_code, 400, "角色只能指定模型")
        self.assertEqual(o.patch(f"/api/admin/models/{vl}", json={"name": "多模态", "price_in": 1}).status_code, 200)
        self.assertEqual(o.delete(f"/api/admin/models/{vl}").status_code, 200)
        roles = o.get("/api/admin/endpoints").json()["roles"]
        self.assertIsNone(roles["planner"], "删除模型后清除它负责的角色")
        self.assertEqual(roles["writer"], chat)
        self.assertEqual(o.delete(f"/api/admin/endpoints/{eid}").status_code, 200)
        self.assertEqual(o.get("/api/admin/endpoints").json()["models"], [], "删除接口同时删除其下的模型")
        self.assertEqual(o.get("/api/admin/system").status_code, 200)
        self.assertEqual(o.get("/api/admin/usage").status_code, 200)
        self.assertEqual(o.get("/api/admin/audit").status_code, 200)

    def test_json_body_size_limited(self):
        """任意大的 JSON 请求体不会被整个读进内存：未登录的登录接口也适用。"""
        c = self.client()
        big = b'{"username": "' + b"a" * (3 * 1024 * 1024) + b'", "password": "x"}'
        r = c.post("/api/login", content=big, headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413, r.text)
        self.assertEqual(r.json()["code"], "too_large")
        o = self.owner()
        r = o.post("/api/admin/tokens", content=big, headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)

    def test_totp_reset_requires_current_code(self):
        """已开启两步验证时，重新设置密钥必须验证当前验证码，与关闭时一致。"""
        from app import db, security
        o = self.owner()
        secret = security.new_totp_secret()
        db.run("UPDATE owner SET totp_secret=?, totp_enabled=1 WHERE id=1", (secret,))
        try:
            self.assertEqual(o.post("/api/owner/totp/setup", json={}).status_code, 400)
            self.assertEqual(o.post("/api/owner/totp/setup", json={"code": "000000"}).status_code, 400)
            self.assertEqual(db.one("SELECT totp_enabled FROM owner WHERE id=1")["totp_enabled"], 1, "失败的重置不能关闭两步验证")
            r = o.post("/api/owner/totp/setup", json={"code": security.totp_now(secret)})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(db.one("SELECT totp_enabled FROM owner WHERE id=1")["totp_enabled"], 0)
        finally:
            db.run("UPDATE owner SET totp_secret=NULL, totp_enabled=0 WHERE id=1")

    def test_admin_upload_limit_applies_to_tokens(self):
        """后台调整的单文件上传上限对令牌用户同样生效；令牌自己的上限只能更小。"""
        from app import db
        o = self.owner()
        old = db.get_setting("max_upload_mb")
        try:
            self.assertEqual(o.put("/api/admin/settings", json={"max_upload_mb": 1}).status_code, 200)
            g, _ = self.guest(o, note="上限")
            self.assertEqual(g.post("/api/uploads", json={"name": "a.txt", "size": 2 * 1024 * 1024}).status_code, 413)
            self.assertEqual(g.post("/api/uploads", json={"name": "a.txt", "size": 512 * 1024}).status_code, 200)
            g2, _ = self.guest(o, note="更大", quota={"upload_mb": 50})
            self.assertEqual(g2.post("/api/uploads", json={"name": "a.txt", "size": 2 * 1024 * 1024}).status_code, 413, "令牌上限不能超过后台上限")
        finally:
            if old is None:
                db.run("DELETE FROM meta WHERE key='setting:max_upload_mb'")
            else:
                db.set_setting("max_upload_mb", old)

    def test_bad_params_are_400_not_500(self):
        o = self.owner()
        for body in ({"kind": "convert", "params": {"target": "pdf"}}, {"kind": "ocr", "params": {}},
                     {"kind": "pdf_tool", "params": {"op": "merge"}}, {"kind": "file_pages", "params": {}}):
            r = o.post("/api/jobs", json=body)
            self.assertEqual(r.status_code, 400, (body, r.text))
        from app import works
        wid = works.create_work(o.get("/api/me").json()["workspace_id"], "doc", "标签")
        self.assertEqual(o.patch(f"/api/works/{wid}", json={"tags": 123}).status_code, 400)
        self.assertEqual(o.patch(f"/api/works/{wid}", json={"tags": ["a", " b "]}).status_code, 200)
        self.assertEqual(o.get(f"/api/works/{wid}").json()["tags"], ["a", "b"])
        from app.scheduler import delete_work_hard
        delete_work_hard(wid)  # 其他用例按作品数量断言

    def test_all_routes_require_auth(self):
        """越权测试：未登录访问所有需要登录的接口都应被拒绝。"""
        from starlette.routing import Route
        c = self.client()
        public = {"/", "/healthz", "/api/me", "/api/login", "/api/redeem"}
        for r in self.app.routes:
            if not isinstance(r, Route) or r.path in public:
                continue
            path = r.path.replace("{index:int}", "0")
            import re
            path = re.sub(r"\{[^}]+\}", "x", path)
            for m in r.methods - {"HEAD"}:
                resp = c.request(m, path, json={} if m != "GET" else None)
                self.assertIn(resp.status_code, (401, 403), f"{m} {path} -> {resp.status_code}")


if __name__ == "__main__":
    unittest.main()
