"""任务：创建（权限、参数校验、配额预占）、列表、取消、重试、确认大纲、进度推送（SSE）。"""
from __future__ import annotations

import asyncio
import json
import time

from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from .. import accounts, db, jobs, models_cfg, quota, works
from ..tools import convert as convmod
from ..util import UserError, new_id
from .common import SESSION_COOKIE, Req, api, disk_blocked, page_args

# 任务类型 → (所需权限, 预估 token)
KINDS = {
    "gen_ppt": ("ppt", None), "gen_doc": ("doc", 45000), "gen_xls": ("xls", 15000),
    "edit": ("edit", 15000), "manual_edit": ("edit", 0), "import_file": ("import", 0), "import_edit": ("edit", 20000),
    "rebuild": ("import", 60000), "convert": ("convert", 0), "pdf_tool": ("pdf", 0), "ocr": ("pdf", 0),
    "model_test": (None, 0), "compat_pack": (None, 0), "file_pages": ("pdf", 0),
}
OWNER_ONLY = {"model_test", "compat_pack"}
NEEDS_MODEL = {"gen_ppt", "gen_doc", "gen_xls", "edit", "import_edit", "rebuild"}
TITLES = {"gen_ppt": "生成 PPT", "gen_doc": "生成 Word", "gen_xls": "生成 Excel", "edit": "对话修改", "manual_edit": "直接编辑",
          "import_file": "导入文件", "import_edit": "原位修改", "rebuild": "重建", "convert": "格式转换", "pdf_tool": "PDF 工具",
          "ocr": "OCR 识别", "model_test": "测试模型接口", "compat_pack": "生成兼容性测试包", "file_pages": "页面缩略图"}
ALLOWED_PARAMS = {
    "gen_ppt": {"topic", "file_ids", "pages", "audience", "purpose", "style", "language", "aspect", "font_mode", "logo_file_id",
                "theme_preset", "theme_file_id", "image_mode", "confirm_outline", "footer", "image_file_ids", "extra",
                "web_search", "urls"},
    "gen_doc": {"topic", "file_ids", "preset", "pages", "audience", "language", "font_mode", "logo_file_id", "image_mode",
                "confirm_outline", "header_text", "extra", "style", "web_search", "urls"},
    "gen_xls": {"topic", "file_ids", "extra"},
    "edit": {"instruction", "scope", "base_version_id", "image_mode"},
    "manual_edit": {"ops", "base_version_id"},
    "import_file": {"file_id", "title"},
    "import_edit": {"instruction", "scope", "base_version_id", "track_changes"},
    "rebuild": {"file_id", "work_id", "target", "instruction", "pages", "preset", "theme_preset", "image_mode", "font_mode", "aspect"},
    "convert": {"file_id", "target", "options"},
    "pdf_tool": {"op", "file_ids", "options"},
    "ocr": {"file_id", "output", "dpi"},
    "model_test": {"endpoint_id", "model_id", "what"},
    "compat_pack": set(),
    "file_pages": {"file_id"},
}


def _files_in_scope(s: accounts.Scope, ids) -> list[str]:
    out = []
    for fid in ids or []:
        f = accounts.get_file_scoped(s, str(fid))
        out.append(f["id"])
    return out


def _estimate(kind: str, p: dict) -> int:
    base = KINDS[kind][1]
    if kind == "gen_ppt":
        return int(p.get("pages") or 12) * 3500 + 10000
    if kind == "rebuild":
        return 60000 if p.get("target") == "doc" else int(p.get("pages") or 12) * 3500 + 20000
    return base or 0


WORK_KINDS = ("edit", "manual_edit", "import_edit")


def create_job(s: accounts.Scope, kind: str, params: dict, work_id: str | None = None, ip: str = "",
               retry_of: dict | None = None) -> dict:
    if kind not in KINDS:
        raise UserError("未知的任务类型")
    if kind not in WORK_KINDS:
        # 只有修改类任务作用于已有作品（下面会检查写权限）。其他任务一律新建作品，不接受外部传入的作品 ID；
        # 唯一例外是重试一个生成任务：沿用它自己创建、仍为空的作品，同样检查写权限。
        work_id = None
        if retry_of and retry_of.get("work_id") and retry_of["kind"] == kind:
            try:
                w0 = accounts.get_work_scoped(s, retry_of["work_id"], write=True)
                if not w0["current_version_id"]:
                    work_id = w0["id"]
            except UserError:
                pass
    if kind in OWNER_ONLY:
        s.require_owner()
    perm = KINDS[kind][0]
    if perm:
        s.require(perm)
    if disk_blocked() and kind not in ("model_test",):
        raise UserError("服务器磁盘空间不足，暂停新任务，请联系管理员清理", 507, "disk_full")
    if kind in NEEDS_MODEL and not models_cfg.status()["text"]:
        raise UserError("尚未配置可用的文字模型接口，请管理员在后台“模型接口”中添加并测试", 503, "no_model")
    p = {k: v for k, v in (params or {}).items() if k in ALLOWED_PARAMS[kind]}
    # 校验参数
    for key in ("file_ids", "image_file_ids"):
        if key in p:
            p[key] = _files_in_scope(s, p[key])[:20]
    for key in ("file_id", "logo_file_id", "theme_file_id"):
        if p.get(key):
            p[key] = _files_in_scope(s, [p[key]])[0]
    if kind in ("convert", "ocr", "file_pages", "import_file") and not p.get("file_id"):
        raise UserError("请选择文件")
    if kind == "pdf_tool" and not p.get("file_ids"):
        raise UserError("请选择 PDF 文件")
    title = TITLES[kind]
    if kind in ("gen_ppt", "gen_doc", "gen_xls"):
        if "urls" in p:
            from ..pipeline import research
            p["urls"] = research.clean_urls(p["urls"])
        if p.get("web_search"):
            from ..pipeline import research
            if not research.enabled():
                raise UserError("管理员没有配置联网检索（DW_SEARXNG_URL）")
            p["web_search"] = True
        if not str(p.get("topic", "")).strip() and not p.get("file_ids") and not p.get("urls"):
            raise UserError("请填写主题、上传资料或给出参考网页")
        p["topic"] = str(p.get("topic", ""))[:4000]
        if p.get("pages") is not None:
            try:
                p["pages"] = max(3, min(60, int(p["pages"])))
            except (TypeError, ValueError):
                p.pop("pages")
        title += "：" + (p["topic"][:30] or "根据资料")
    if kind in ("edit", "manual_edit", "import_edit"):
        if not work_id:
            raise UserError("缺少作品")
        w = accounts.get_work_scoped(s, work_id, write=True)
        if w["kind"].startswith("import_") != (kind == "import_edit"):
            raise UserError("导入的作品请使用原位修改；AI 作品请使用对话修改")
        if kind != "manual_edit" and not str(p.get("instruction", "")).strip():
            raise UserError("请输入修改指令")
        if not p.get("base_version_id"):
            p["base_version_id"] = w["current_version_id"]
        elif p["base_version_id"] != w["current_version_id"]:
            raise UserError("作品已经有了新版本（可能在另一个窗口修改过），请刷新后在最新版本上操作", 409, "conflict")
        if kind == "manual_edit" and not isinstance(p.get("ops"), list):
            raise UserError("缺少修改内容")
        title += "：" + (str(p.get("instruction", ""))[:30] if kind != "manual_edit" else w["title"][:30])
    if kind == "rebuild":
        if p.get("work_id") and not p.get("file_id"):
            w = accounts.get_work_scoped(s, p["work_id"])
            v = works.current_version(w["id"])
            if not v or not v["file_sha"]:
                raise UserError("只有导入的作品可以重建")
            from .. import storage
            ext = (v["manifest"] or {}).get("file_kind", "pptx")
            f = storage.create_file_record(s.workspace_id, v["file_sha"], 0, f"{w['title']}.{ext}", "upload",
                                           meta={"kind": ext}, ttl=7 * 86400)
            p["file_id"] = f["id"]
        if not p.get("file_id"):
            raise UserError("请选择要重建的文件")
        if p.get("target") not in ("ppt", "doc"):
            raise UserError("重建目标只能是 PPT 或 Word")
        p.pop("work_id", None)
    if kind == "convert":
        f = accounts.get_file_scoped(s, p["file_id"])
        k = (f.get("meta") or {}).get("kind")
        if p.get("target") == "pptx_rebuild":
            raise UserError("PDF 重建为可编辑 PPT 请使用“重建”功能")
        if not any(t["target"] == p.get("target") for t in convmod.targets_for(k or "")):
            raise UserError("不支持该转换")
    if kind == "pdf_tool" and p.get("op") not in ("merge", "split", "compress", "rotate", "reorder", "extract_text", "extract_images"):
        raise UserError("未知的 PDF 操作")
    est = _estimate(kind, p)
    jid = new_id("j_")
    conn = db.connect()
    with db.tx(conn):
        quota.reserve(jid, s.token_id, kind, est, conn=conn)
        j = jobs.enqueue(kind, s.workspace_id, p, token_id=s.token_id, work_id=work_id, title=title, job_id=jid, conn=conn)
    accounts.audit(s, "job_created", jid, {"kind": kind}, ip)
    return jobs.public(j)


@api(auth="any")
def job_create(req: Req):
    return create_job(req.s, str(req.b("kind", "")), req.b("params") or {}, req.b("work_id"), req.ip)


MAX_WATCH = 100  # 一个页面同时关注的任务数上限（推送与批量轮询）


def _watch_ids(req: Req) -> list[str]:
    ids = [x.strip() for x in str(req.q("ids", "")).split(",") if x.strip()]
    return list(dict.fromkeys(ids))[:MAX_WATCH]


@api(auth="any")
def jobs_list(req: Req):
    if req.q("ids") is not None:
        # 批量查询指定任务（推送不可用时前端一次轮询一个页面上的全部任务）；无权查看的任务不返回
        out = []
        for jid in _watch_ids(req):
            try:
                accounts.get_job_scoped(req.s, jid)
            except UserError:
                continue
            cur = _snapshot(jid)
            if cur:
                out.append(cur)
        return {"items": out}
    limit, offset = page_args(req)
    conds, args = ["parent_id IS NULL"], []
    if not (req.s.is_owner and req.q("all") == "1"):
        conds.append("workspace_id=?")
        args.append(req.s.workspace_id)
    st = req.q("status")
    if st == "active":
        conds.append("status IN ('queued','running','cancelling','awaiting_input')")
    elif st:
        conds.append("status=?")
        args.append(st)
    if req.q("work_id"):
        conds.append("work_id=?")
        args.append(req.q("work_id"))
    rows = db.all_(f"SELECT * FROM jobs WHERE {' AND '.join(conds)} ORDER BY created_at DESC LIMIT ? OFFSET ?", args + [limit, offset])
    out = []
    for r in rows:
        r = jobs._decode(r)
        pj = jobs.public(r)
        pj["stream"] = ""
        pj["result"] = _slim_result(pj["result"])
        out.append(pj)
    return {"items": out}


def _slim_result(r):
    if isinstance(r, dict) and ("outline" in r or "sources" in r):
        return {k: v for k, v in r.items() if k not in ("sources", "src_images")}
    return r


@api(auth="any")
def job_get(req: Req):
    j = accounts.get_job_scoped(req.s, req.path["jid"])
    pj = jobs.public(j)
    pj["result"] = _slim_result(pj["result"])
    pj["children"] = [jobs.public(jobs._decode(c)) | {"result": None} for c in db.all_("SELECT * FROM jobs WHERE parent_id=? ORDER BY created_at", (j["id"],))]
    return pj


@api(auth="any")
def job_cancel(req: Req):
    j = accounts.get_job_scoped(req.s, req.path["jid"], write=True)
    if j["status"] in jobs.FINAL:
        raise UserError("任务已结束")
    jobs.request_cancel(j["id"], "用户取消")
    if j["status"] in ("queued", "awaiting_input"):
        quota.settle(j["id"])
    return {"ok": True}


@api(auth="any")
def job_retry(req: Req):
    j = accounts.get_job_scoped(req.s, req.path["jid"], write=True)
    if j["status"] not in ("failed", "cancelled"):
        raise UserError("只有失败或已取消的任务可以重试")
    p = dict(j["params"])
    if j["kind"] in ("edit", "manual_edit", "import_edit"):
        p.pop("base_version_id", None)
    return create_job(req.s, j["kind"], p, j["work_id"], req.ip, retry_of=j)


@api(auth="any")
def job_continue(req: Req):
    j = accounts.get_job_scoped(req.s, req.path["jid"], write=True)
    outline = req.b("outline")
    if not isinstance(outline, dict) or not outline.get("slides") and not outline.get("sections"):
        raise UserError("大纲格式不正确")
    return jobs.public(jobs.resume(j["id"], {"outline": outline, "confirm_outline": False}))


SSE_AUTH_RECHECK = 5  # 秒：推送过程中复查会话与令牌授权的间隔


@api(auth="any", raw_body=True)
async def job_events(req: Req):
    """SSE：单个任务状态变化时推送（旧接口，保留兼容）。"""
    j = await run_in_threadpool(accounts.get_job_scoped, req.s, req.path["jid"])
    return _sse(req, [j["id"]])


@api(auth="any", raw_body=True)
async def jobs_events(req: Req):
    """SSE：一个连接推送多个任务（?ids=a,b,c）。浏览器对同一域名的连接数有限，
    一个页面只开一条流，页面上的任务卡片都从这条流取更新。无权查看的任务直接忽略。"""
    ids = _watch_ids(req)
    if not ids:
        raise UserError("缺少任务 ID")

    def allowed() -> list[str]:
        out = []
        for jid in ids:
            try:
                accounts.get_job_scoped(req.s, jid)
                out.append(jid)
            except UserError:
                pass
        return out
    ok = await run_in_threadpool(allowed)
    if not ok:
        raise UserError("任务不存在", 404, "not_found")
    return _sse(req, ok)


def _sse(req: Req, ids: list[str]) -> StreamingResponse:
    """每 0.6 秒查一次这些任务（一条 SQL），有变化才推送；每 15 秒心跳；
    会话或令牌失效时发送 denied 并结束；全部任务结束后关闭。"""
    cookie = req.request.cookies.get(SESSION_COOKIE)

    def still_allowed() -> list[str]:
        # 连接建立后仍然按当前会话和令牌状态复查：撤销、到期、退出登录后立即结束推送
        sc = accounts.session_scope(cookie)
        if sc is None:
            return []
        out = []
        for jid in ids:
            try:
                accounts.get_job_scoped(sc, jid)
                out.append(jid)
            except UserError:
                pass
        return out

    async def gen():
        nonlocal ids
        last: dict[str, tuple] = {}
        finished: set[str] = set()
        last_beat = time.time()
        last_auth = time.time()
        while True:
            if await req.request.is_disconnected():
                break
            if time.time() - last_auth > SSE_AUTH_RECHECK:
                last_auth = time.time()
                ids = await run_in_threadpool(still_allowed)
                if not ids:
                    yield "event: denied\ndata: {}\n\n"
                    break
            snaps = await run_in_threadpool(_snapshot_many, ids)
            sent = False
            for cur in snaps:
                key = (cur["updated_at"], cur["status"], cur["progress"], len(cur["stream"]))
                if key != last.get(cur["id"]):
                    last[cur["id"]] = key
                    yield f"event: job\ndata: {json.dumps(cur, ensure_ascii=False)}\n\n"
                    sent = True
                if cur["status"] in jobs.FINAL or cur["status"] == "awaiting_input":
                    finished.add(cur["id"])
            got = {c["id"] for c in snaps}
            finished |= {jid for jid in ids if jid not in got}  # 已删除的任务
            if sent:
                last_beat = time.time()
            if all(jid in finished for jid in ids):
                break
            if not sent and time.time() - last_beat > 15:
                yield ": ping\n\n"
                last_beat = time.time()
            await asyncio.sleep(0.6)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


def _snapshot_many(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = db.all_(f"SELECT * FROM jobs WHERE id IN ({marks})", ids)
    kids = db.all_(f"SELECT parent_id, stage, message, status, progress FROM jobs WHERE parent_id IN ({marks}) "
                   "AND status IN ('running','queued') ORDER BY created_at", ids)
    child: dict[str, dict] = {}
    for k in kids:  # 同一父任务取最新的一个
        child[k["parent_id"]] = {x: k[x] for x in ("stage", "message", "status", "progress")}
    out = []
    for r in rows:
        pj = jobs.public(jobs._decode(r))
        pj["result"] = _slim_result(pj["result"])
        if r["id"] in child:
            pj["child"] = child[r["id"]]
        out.append(pj)
    return out


def _snapshot(jid: str) -> dict | None:
    j = jobs.get(jid)
    if not j:
        return None
    pj = jobs.public(j)
    pj["result"] = _slim_result(pj["result"])
    kids = db.all_("SELECT stage, message, status, progress FROM jobs WHERE parent_id=? AND status IN ('running','queued') ORDER BY created_at DESC LIMIT 1", (jid,))
    if kids:
        pj["child"] = kids[0]
    return pj
