"""对话修改与直接编辑（AI 作品）。

- 修改基于提交时的版本号；期间已有新版本时拒绝执行，不会悄悄覆盖。
- 模型只输出限定操作，每个操作指明目标 ID；范围之外的目标会被拒绝并反馈给模型修正。
- PPT 修改后只对变化的页面做排版检查与修正；预览按页缓存，只渲染变化的页。
"""
from __future__ import annotations

import json

from .. import db, works
from ..spec.deck import Deck, deck_text
from ..spec.document import Document, document_text
from ..spec.ops import OpError, apply_deck_ops, apply_doc_ops, apply_wb_ops, deck_diff, doc_diff
from ..spec.workbook import Workbook, workbook_text
from ..util import UserError
from . import prompts
from .common import page_cache_of, render, resolve_images, save_version
from .context import Ctx
from .ppt import render_and_fix

LARGE_DECK = 12
LARGE_DOC = 40


def _base(ctx: Ctx) -> tuple[dict, dict]:
    w = db.one("SELECT * FROM works WHERE id=?", (ctx.work_id,))
    if not w or w["deleted_at"]:
        raise UserError("作品不存在或已删除")
    cur = works.current_version(ctx.work_id)
    base = ctx.p.get("base_version_id")
    if base and cur and cur["id"] != base:
        # 重试时：本任务已经产生的版本不算冲突
        mine = db.one("SELECT id FROM versions WHERE job_id=?", (ctx.id,))
        if not mine:
            raise UserError("作品已经有了新版本（可能在另一个窗口修改过），请在最新版本上重新执行这条指令", 409, "conflict")
    return w, cur


def handle_edit(ctx: Ctx):
    w, cur = _base(ctx)
    kind = w["kind"]
    instr = ctx.p["instruction"]
    scope = ctx.p.get("scope") or {"type": "all"}
    ctx.progress(0.05, "理解指令")
    if kind == "ppt":
        return _edit_ppt(ctx, w, cur, instr, scope)
    if kind == "doc":
        return _edit_doc(ctx, w, cur, instr, scope)
    if kind == "xls":
        return _edit_xls(ctx, w, cur, instr, scope)
    raise UserError("导入的作品请使用原位修改")


def _scope_desc(scope: dict) -> str:
    t = scope.get("type", "all")
    if t == "all":
        return "修改范围：整份文档"
    if t == "range":
        return f"修改范围：只能修改单元格区域 {scope.get('range')}"
    return f"修改范围：只能修改这些目标 {json.dumps(scope.get('ids'), ensure_ascii=False)}"


# ---------- PPT ----------

def _edit_ppt(ctx: Ctx, w, cur, instr, scope):
    deck = Deck.model_validate(cur["spec"])
    assets = dict((cur["manifest"] or {}).get("assets") or {})
    stype = scope.get("type", "all")
    if stype == "slides":
        focus = set(scope.get("ids") or [])
    elif stype == "elements":
        focus = {x.partition("|")[0] for x in scope.get("ids") or []}
    else:
        focus = None
    batches = [None]
    if focus is None and len(deck.slides) > LARGE_DECK:
        ids = [s.id for s in deck.slides]
        batches = [ids[i:i + 8] for i in range(0, len(ids), 8)]
    new_deck = deck
    replies = []
    for bi, batch in enumerate(batches):
        ctx.check()
        ctx.progress(0.1 + 0.4 * bi / len(batches), "修改中", f"第 {bi + 1}/{len(batches)} 批" if len(batches) > 1 else None)
        show = focus if focus is not None else (set(batch) if batch else None)
        user = _ppt_context(new_deck, show) + f"\n\n{_scope_desc(scope)}\n用户指令：{instr}"
        if batch:
            user += f"\n（文档较长，本次只处理上面完整列出的页面：{', '.join(batch)}；其余页面由其他批次处理。）"
        base_for_validate = new_deck

        def validate(d, base=base_for_validate, batch=batch):
            if not isinstance(d, dict) or not isinstance(d.get("ops"), list):
                raise ValueError("需要 ops 数组")
            sc = scope if not batch else {"type": "slides", "ids": batch, "allow_global": True}
            try:
                nd, ch = apply_deck_ops(base, d["ops"], sc)
            except OpError as e:
                raise ValueError(e.message)
            except (KeyError, TypeError) as e:
                raise ValueError(f"操作缺少字段：{e}")
            return nd, ch, d.get("reply", "")
        new_deck, _, reply = ctx.llm.json("planner", prompts.EDIT_PPT, user, validate=validate, stream=True, max_tokens=8000)
        if reply:
            replies.append(reply)
    diff = deck_diff(deck, new_deck)
    changed = diff["added"] + diff["modified"]
    if diff["theme_changed"] or diff["settings_changed"] or diff["order_changed"]:
        changed = [s.id for s in new_deck.slides]
    if new_deck == deck:
        return {"work_id": w["id"], "no_change": True, "reply": "；".join(replies) or "没有需要修改的内容"}
    dd = new_deck.model_dump(mode="json")
    resolve_images(ctx, dd, assets, ctx.p.get("image_mode", "auto"), lo=0.5, hi=0.55)
    new_deck = Deck.model_validate(dd)
    new_deck, rendered, log = render_and_fix(ctx, new_deck, assets, w["title"], 0.55, 0.95, page_cache=page_cache_of(cur),
                                             only=changed, step="render")
    v = save_version(ctx, w["id"], "ppt", new_deck.model_dump(mode="json"), rendered, assets, source="chat", message=instr,
                     changed=changed, base_version_id=cur["id"], title=w["title"], search_text=deck_text(new_deck),
                     extra={"reply": "；".join(replies), "fix_log": log, "removed": diff["removed"],
                            "warnings": rendered.get("warnings", [])})
    return {"work_id": w["id"], "version_id": v["id"], "changed": changed, "reply": "；".join(replies)}


def _ppt_context(deck: Deck, focus: set | None) -> str:
    lines = [f"演示标题：{deck.title}", f"主题：{json.dumps(deck.theme.model_dump(), ensure_ascii=False)}", "全部页面："]
    for i, s in enumerate(deck.slides):
        if focus is None or s.id in focus:
            lines.append(f"[{i}] {json.dumps(s.model_dump(mode='json'), ensure_ascii=False)}")
        else:
            lines.append(f"[{i}] id={s.id} layout={s.layout} 标题：{s.title}")
    return "\n".join(lines)


# ---------- Word ----------

def _edit_doc(ctx: Ctx, w, cur, instr, scope):
    doc = Document.model_validate(cur["spec"])
    assets = dict((cur["manifest"] or {}).get("assets") or {})
    focus = set(scope.get("ids") or []) if scope.get("type") == "blocks" else None
    batches = [None]
    if focus is None and len(doc.blocks) > LARGE_DOC:
        ids = [b.id for b in doc.blocks]
        batches = [ids[i:i + 30] for i in range(0, len(ids), 30)]
    new_doc = doc
    replies = []
    for bi, batch in enumerate(batches):
        ctx.check()
        ctx.progress(0.1 + 0.5 * bi / len(batches), "修改中", f"第 {bi + 1}/{len(batches)} 批" if len(batches) > 1 else None)
        show = focus if focus is not None else (set(batch) if batch else None)
        user = _doc_context(new_doc, show) + f"\n\n{_scope_desc(scope)}\n用户指令：{instr}"
        if batch:
            user += "\n（文档较长，本次只处理上面完整列出内容的块；其余块由其他批次处理。）"
        base_for_validate = new_doc

        def validate(d, base=base_for_validate, batch=batch):
            if not isinstance(d, dict) or not isinstance(d.get("ops"), list):
                raise ValueError("需要 ops 数组")
            sc = scope if not batch else {"type": "blocks", "ids": batch, "allow_global": True}
            try:
                nd, _ = apply_doc_ops(base, d["ops"], sc)
            except OpError as e:
                raise ValueError(e.message)
            except (KeyError, TypeError) as e:
                raise ValueError(f"操作缺少字段：{e}")
            return nd, d.get("reply", "")
        new_doc, reply = ctx.llm.json("writer", prompts.EDIT_DOC, user, validate=validate, stream=True, max_tokens=8000)
        if reply:
            replies.append(reply)
    diff = doc_diff(doc, new_doc)
    changed = diff["added"] + diff["modified"]
    if not changed and not diff["removed"] and new_doc == doc:
        return {"work_id": w["id"], "no_change": True, "reply": "；".join(replies) or "没有需要修改的内容"}
    dd = new_doc.model_dump(mode="json")
    resolve_images(ctx, dd, assets, ctx.p.get("image_mode", "auto"), lo=0.6, hi=0.65)
    new_doc = Document.model_validate(dd)
    ctx.progress(0.7, "排版与导出")
    theme = (cur["manifest"] or {}).get("theme")
    rendered = render(ctx, "doc", new_doc.model_dump(mode="json"), assets, "render_0", title=w["title"], theme=theme)
    v = save_version(ctx, w["id"], "doc", new_doc.model_dump(mode="json"), rendered, assets, source="chat", message=instr,
                     changed=changed, base_version_id=cur["id"], title=w["title"], search_text=document_text(new_doc),
                     extra={"reply": "；".join(replies), "removed": diff["removed"], "theme": theme,
                            "warnings": rendered.get("warnings", [])})
    return {"work_id": w["id"], "version_id": v["id"], "changed": changed, "reply": "；".join(replies)}


def _doc_context(doc: Document, focus: set | None) -> str:
    lines = [f"文档标题：{doc.title}（类型 {doc.preset}）", "全部内容块："]
    for b in doc.blocks:
        d = b.model_dump(mode="json")
        if focus is None or b.id in focus:
            lines.append(json.dumps(d, ensure_ascii=False))
        else:
            label = (d.get("text") or d.get("caption") or d.get("latex") or "")[:60]
            lines.append(f"id={b.id} type={b.type}{' level=' + str(d.get('level')) if d.get('level') else ''} {label}")
    return "\n".join(lines)


# ---------- Excel ----------

def _edit_xls(ctx: Ctx, w, cur, instr, scope):
    wb = Workbook.model_validate(cur["spec"])
    preview = (cur["manifest"] or {}).get("preview") or {}
    parts = [f"工作簿：{wb.title}"]
    for s in wb.sheets:
        d = s.model_dump(mode="json")
        rows = d["rows"]
        d["rows"] = rows[:150]
        parts.append(f"工作表“{s.name}”（{len(rows)} 行数据{'，只列出前 150 行' if len(rows) > 150 else ''}）：\n{json.dumps(d, ensure_ascii=False)}")
        pv = preview.get(s.name)
        if pv:
            parts.append(f"“{s.name}”计算后的值（前 30 行）：{json.dumps(pv['rows'][:30], ensure_ascii=False)}")
    user = "\n\n".join(parts) + f"\n\n{_scope_desc(scope)}\n用户指令：{instr}"

    def validate(d):
        if not isinstance(d, dict) or not isinstance(d.get("ops"), list):
            raise ValueError("需要 ops 数组")
        try:
            nw, ch = apply_wb_ops(wb, d["ops"], scope)
        except OpError as e:
            raise ValueError(e.message)
        except (KeyError, TypeError) as e:
            raise ValueError(f"操作缺少字段：{e}")
        return nw, ch, d.get("reply", "")
    ctx.progress(0.1, "修改中")
    new_wb, changed, reply = ctx.llm.json("planner", prompts.EDIT_XLS, user, validate=validate, stream=True, max_tokens=8000)
    ctx.progress(0.6, "重算与验证")
    rendered = render(ctx, "xls", new_wb.model_dump(mode="json"), {}, "render_0", title=w["title"])
    v = save_version(ctx, w["id"], "xls", new_wb.model_dump(mode="json"), rendered, {}, source="chat", message=instr,
                     changed=changed, base_version_id=cur["id"], title=w["title"], search_text=workbook_text(new_wb),
                     extra={"reply": reply})
    return {"work_id": w["id"], "version_id": v["id"], "changed": changed, "reply": reply}


# ---------- 直接编辑（不经过模型） ----------

def handle_manual_edit(ctx: Ctx):
    """ops 与对话修改相同的限定操作，不经过模型；每次保存生成一个新版本。"""
    w, cur = _base(ctx)
    ops = ctx.p["ops"]
    kind = w["kind"]
    assets = dict((cur["manifest"] or {}).get("assets") or {})
    ctx.progress(0.1, "应用修改")
    if kind == "ppt":
        spec, changed = apply_deck_ops(Deck.model_validate(cur["spec"]), ops)
        rendered = render(ctx, "ppt", spec.model_dump(mode="json"), assets, "render_0", title=w["title"], page_cache=page_cache_of(cur))
        text = deck_text(spec)
    elif kind == "doc":
        spec, changed = apply_doc_ops(Document.model_validate(cur["spec"]), ops)
        rendered = render(ctx, "doc", spec.model_dump(mode="json"), assets, "render_0", title=w["title"], theme=(cur["manifest"] or {}).get("theme"))
        text = document_text(spec)
    elif kind == "xls":
        spec, changed = apply_wb_ops(Workbook.model_validate(cur["spec"]), ops)
        rendered = render(ctx, "xls", spec.model_dump(mode="json"), {}, "render_0", title=w["title"])
        text = workbook_text(spec)
    else:
        raise UserError("导入的作品请使用原位修改")
    sd = spec.model_dump(mode="json")
    # 版本不可变：每次直接编辑都生成新版本，可以逐次恢复。版本数量由定时清理控制（星标版本始终保留）。
    extra = {k: cur["manifest"].get(k) for k in ("theme", "outline") if (cur["manifest"] or {}).get(k)}
    v = save_version(ctx, w["id"], kind, sd, rendered, assets, source="manual", message="直接编辑", changed=changed,
                     base_version_id=cur["id"], title=w["title"], search_text=text, extra=extra)
    return {"work_id": w["id"], "version_id": v["id"], "changed": changed}


def _strip(man: dict) -> dict:
    from ..works import _strip_file_ids
    return _strip_file_ids(man)
