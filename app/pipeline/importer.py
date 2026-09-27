"""已有文件：导入（检查、剥离宏、旧格式转换、预览、可修改单元与导入报告）、原位修改、重建。"""
from __future__ import annotations

import json
from pathlib import Path

from .. import db, storage, works
from ..tools import inplace, limits, office, ooxml
from ..tools import pdf as pdftools
from ..util import UserError, safe_filename, stable_hash
from . import prompts
from .context import Ctx

IMPORT_KIND = {"pptx": "import_pptx", "docx": "import_docx", "xlsx": "import_xlsx"}
PRESERVED_LABELS = {
    "pptx": ["smartart", "embeddings", "animations", "media"],
    "docx": ["textboxes", "fields", "content_controls", "comments", "embeddings", "equations"],
    "xlsx": ["pivot_tables", "slicers", "external_links", "charts", "drawings"],
}


# ---------- 预览（preview 队列） ----------

def handle_preview(ctx: Ctx) -> dict:
    """文件 → PDF → 页面图片。按文件内容哈希缓存。"""
    sha, name = ctx.p["sha"], ctx.p.get("name") or "file"
    src = storage.materialize(sha, ctx.tmp / "in", safe_filename(name))
    pdf = src if src.suffix.lower() == ".pdf" else office.to_pdf(src, ctx.tmp / "pdf", cancel=ctx.cancelled)
    imgs = pdftools.render_pages(pdf, ctx.tmp / "prev", dpi=100, max_side=1400, cancel=ctx.cancelled)
    pages = []
    for i, p in enumerate(imgs):
        s, size = storage.put_file(p)
        pages.append({"id": str(i + 1), "hash": s[:24], "preview": s, "size": size})
    psha, psize = storage.put_file(pdf)
    return {"pages": pages, "pdf": {"sha": psha, "size": psize, "name": Path(name).stem + ".pdf"}}


def handle_version_preview(ctx: Ctx) -> dict:
    """重新生成被清理过的历史版本预览：优先使用版本保存的 PDF，没有时由原文件转换。页面 ID 和哈希不变。"""
    v = works.get_version(ctx.p["version_id"])
    if not v or not (v["manifest"] or {}).get("previews_pruned"):
        return {"skipped": True}
    man = v["manifest"]
    ex = man.get("exports") or {}
    if ex.get("pdf", {}).get("sha"):
        pdf = storage.materialize(ex["pdf"]["sha"], ctx.tmp / "in", "version.pdf")
    else:
        main = next((ex[k] for k in ("pptx", "docx", "xlsx") if ex.get(k, {}).get("sha")), None)
        sha = main["sha"] if main else v["file_sha"]
        if not sha:
            raise UserError("该版本没有可用于生成预览的文件")
        name = main["name"] if main else f"version.{man.get('file_kind', 'pptx')}"
        src = storage.materialize(sha, ctx.tmp / "in", safe_filename(name))
        pdf = office.to_pdf(src, ctx.tmp / "pdf", cancel=ctx.cancelled)
    imgs = pdftools.render_pages(pdf, ctx.tmp / "prev", dpi=110, max_side=1600, cancel=ctx.cancelled)
    old = man.get("pages") or []
    pages = []
    for i, img in enumerate(imgs):
        s_, size = storage.put_file(img)
        base = dict(old[i]) if len(old) == len(imgs) else {"id": str(i + 1), "hash": s_[:24]}
        base.update({"preview": s_, "size": size})
        pages.append(base)
    works.set_previews(v["id"], pages)
    return {"version_id": v["id"], "pages": len(pages)}


# ---------- 导入（convert 队列） ----------

def handle_import_file(ctx: Ctx) -> dict:
    f = storage.get_file(ctx.p["file_id"])
    if not f:
        raise UserError("文件不存在")
    path = ctx.materialize(f)
    info = limits.inspect(path, f["name"])
    kind = info["kind"]
    notes = []
    if kind in ("docm", "pptm", "xlsm"):
        plain = ooxml.PLAIN_EXT[kind]
        path = ooxml.strip_macros(path, ctx.tmp / f"{Path(f['name']).stem}.{plain}")
        notes.append("文件中的宏已被移除，导入后保存为不含宏的格式")
        kind = plain
    if kind in ("doc", "odt", "rtf", "ppt", "odp", "xls", "ods"):
        path = office.normalize_to_ooxml(path, ctx.tmp / "norm", cancel=ctx.cancelled)
        notes.append(f"旧格式已转换为 {path.suffix[1:].upper()}，部分特殊效果可能丢失")
        kind = path.suffix[1:].lower()
    if kind not in IMPORT_KIND:
        raise UserError("该文件只能使用“重建”方式处理（PDF 和图片没有可原位修改的结构）")
    ctx.progress(0.3, "分析文件")
    units = inplace.units_for(kind, path)
    inv = ooxml.inventory(path)
    sha, size = storage.put_file(path)
    title = ctx.p.get("title") or Path(f["name"]).stem
    ctx.progress(0.5, "生成预览")
    prev = ctx.child("preview", "preview", {"sha": sha, "name": f"{title}.{kind}"})
    report = import_report(kind, units, inv, notes)
    work_id = works.create_work(ctx.workspace_id, IMPORT_KIND[kind], title, source="import", origin_file_id=f["id"])
    db.update("jobs", {"id": ctx.id}, {"work_id": work_id})
    manifest = {"title": title, "exports": {kind: {"sha": sha, "size": size, "name": f"{title}.{kind}"}, "pdf": prev["pdf"]},
                "pages": prev["pages"], "units_count": len(units), "inventory": inv, "report": report, "file_kind": kind,
                "warnings": notes}
    text = "\n".join(u.get("text", "") for u in units)
    v = works.add_version(work_id, job_id=ctx.id, source="import", message=f"导入 {f['name']}", file_sha=sha,
                          manifest=manifest, title=title, search_text=text)
    return {"work_id": work_id, "version_id": v["id"], "report": report}


def import_report(kind: str, units: list[dict], inv: dict, notes: list[str]) -> dict:
    counts: dict[str, int] = {}
    for u in units:
        counts[u["kind"]] = counts.get(u["kind"], 0) + 1
    labels = {"text": "文字段落", "table": "表格单元格", "chart": "原生图表", "notes": "演讲备注", "header": "页眉", "footer": "页脚", "cell": "单元格"}
    editable = [f"{labels.get(k, k)} {v} 处" for k, v in counts.items()]
    preserved = [f"{ooxml.LABELS.get(k, k)} {inv[k]} 个" for k in PRESERVED_LABELS.get(kind, []) if inv.get(k)]
    return {"editable": editable, "preserved": preserved, "notes": notes,
            "summary": f"可修改：{'、'.join(editable) or '无'}；原样保留：{'、'.join(preserved) or '无'}"}


# ---------- 原位修改（ai 队列） ----------

def handle_import_edit(ctx: Ctx) -> dict:
    from .edit import _base

    w, cur = _base(ctx)
    kind = (cur["manifest"] or {}).get("file_kind") or w["kind"].replace("import_", "")
    src = storage.materialize(cur["file_sha"], ctx.tmp / "in", f"src.{kind}")
    units = inplace.units_for(kind, src)
    scope = ctx.p.get("scope") or {"type": "all"}
    if not isinstance(scope, dict) or scope.get("type", "all") not in ("all", "slides", "units"):
        raise UserError("不支持的修改范围")
    if scope.get("type") == "slides" and kind != "pptx":
        raise UserError("只有 PPT 支持按页面选择修改范围")
    if scope.get("type") == "slides" and kind == "pptx":
        keep = set(int(x) for x in scope.get("ids") or [])
        units = [u for u in units if u.get("slide") in keep]
    elif scope.get("type") == "units":
        keep = set(scope.get("ids") or [])
        units = [u for u in units if u["id"] in keep]
    allowed = {u["id"] for u in units}
    instr = ctx.p["instruction"]
    all_ops, replies = [], []
    batches = [units[i:i + 120] for i in range(0, len(units), 120)] or [[]]
    for bi, batch in enumerate(batches):
        ctx.check()
        ctx.progress(0.1 + 0.5 * bi / len(batches), "修改中", f"第 {bi + 1}/{len(batches)} 批" if len(batches) > 1 else None)
        slim = [{k: v for k, v in u.items() if k in ("id", "kind", "text", "slide", "style", "categories", "series")} for u in batch]
        user = (f"文件类型：{kind.upper()}\n修改范围：{'整份文件' if scope.get('type', 'all') == 'all' else '只能修改下面列出的单元'}\n"
                f"可修改的内容单元：\n{json.dumps(slim, ensure_ascii=False)}\n\n用户指令：{instr}")
        if len(batches) > 1:
            user += "\n（文件较长，本次只处理上面列出的单元。结构操作（删页、移动）只在第一批输出。）"

        def validate(d, first=(bi == 0)):
            if not isinstance(d, dict) or not isinstance(d.get("ops"), list):
                raise ValueError("需要 ops 数组")
            for op in d["ops"]:
                if op.get("unit") and op["unit"] not in allowed:
                    raise ValueError(f"单元 {op['unit']} 不在可修改范围内")
                if op.get("op") in ("delete_slide", "duplicate_slide", "move_slide", "set_notes"):
                    if kind != "pptx":
                        raise ValueError("只有 PPT 支持页面操作")
                    if scope.get("type") not in (None, "all") and op.get("op") != "set_notes":
                        raise ValueError("选中部分内容时不能删除或移动页面")
                    if op.get("op") == "set_notes":
                        index = int(op.get("index", 0))
                        if index < 1:
                            raise ValueError("备注页码必须从 1 开始")
                        if scope.get("type") == "slides" and index not in keep:
                            raise ValueError("备注修改超出了选中的页面范围")
                        if scope.get("type") == "units" and f"s{index}/notes" not in allowed:
                            raise ValueError("备注修改超出了选中的单元范围")
                    if not first:
                        raise ValueError("结构操作只能在第一批输出")
            return d
        d = ctx.llm.json("writer", prompts.IMPORT_EDIT, user, validate=validate, stream=True, max_tokens=8000)
        all_ops += d["ops"]
        if d.get("reply"):
            replies.append(d["reply"])
    if not all_ops:
        return {"work_id": w["id"], "no_change": True, "reply": "；".join(replies) or "没有需要修改的内容"}
    ctx.progress(0.65, "写回文件")
    dst = ctx.tmp / f"{safe_filename(w['title'])}.{kind}"
    res = inplace.apply(kind, src, dst, all_ops, track=bool(ctx.p.get("track_changes")))
    before, after = ooxml.inventory(src), ooxml.inventory(dst)
    exp = {"slides": res.get("deleted_slides", 0) * -1 + sum(1 for o in all_ops if o.get("op") == "duplicate_slide")}
    warns = ooxml.compare_inventory(before, after, exp)
    problems = ooxml.validate_package(dst)
    if problems:
        raise UserError("写回后的文件结构校验失败，已放弃本次修改：" + "；".join(problems[:3]))
    sha, size = storage.put_file(dst)
    ctx.progress(0.75, "生成预览")
    prev = ctx.child("preview", "preview", {"sha": sha, "name": dst.name})
    old_pages = {p["hash"]: p for p in (cur["manifest"] or {}).get("pages", [])}
    changed = [p["id"] for p in prev["pages"] if p["hash"] not in old_pages]
    man = dict(cur["manifest"])
    man.update({"exports": {kind: {"sha": sha, "size": size, "name": dst.name}, "pdf": prev["pdf"]}, "pages": prev["pages"],
                "inventory": after, "warnings": [f"元素变化：{w_}" for w_ in warns] + res["skipped"],
                "reply": "；".join(replies), "track_changes": bool(ctx.p.get("track_changes"))})
    man = works._strip_file_ids(man)
    units2 = inplace.units_for(kind, dst)
    v = works.add_version(w["id"], job_id=ctx.id, source="inplace", message=instr, file_sha=sha, manifest=man, changed=changed,
                          base_version_id=cur["id"], search_text="\n".join(u.get("text", "") for u in units2))
    return {"work_id": w["id"], "version_id": v["id"], "applied": res["applied"], "skipped": res["skipped"],
            "warnings": warns, "reply": "；".join(replies)}


# ---------- 重建（ai 队列） ----------

def handle_rebuild(ctx: Ctx) -> dict:
    """抽取文件内容，按本系统布局重新生成 PPT 或 Word。统计文字覆盖率。"""
    from ..pipeline.check import norm
    from collections import Counter

    target = ctx.p.get("target", "ppt")
    fid = ctx.p["file_id"]
    base_instr = ("把资料完整地重建为一份演示文稿：保留原文的全部要点、数据和结构，不要删减内容，可以重新组织版式。"
                  if target == "ppt" else "把资料完整地重建为一份 Word 文档：保留原文的全部内容、标题层级、表格和数据，不要删减。")
    instr = ctx.p.get("instruction") or ""
    ctx.p["topic"] = base_instr + (f"\n用户补充要求：{instr}" if instr else "")
    ctx.p["file_ids"] = [fid]
    if target == "ppt":
        from .ppt import handle_gen_ppt
        res = handle_gen_ppt(ctx)
    else:
        from .doc import handle_gen_doc
        ctx.p.setdefault("preset", "report")
        res = handle_gen_doc(ctx)
    if not isinstance(res, dict):
        return res
    # 文字覆盖率（资料中的文字有多少出现在重建结果中）
    ex = ctx.child("extract", f"extract_{fid}", {"file_id": fid})
    v = works.get_version(res["version_id"])
    from ..spec.common import text_of
    src_c = Counter(norm(ex.get("text", "")))
    out_c = Counter(norm(text_of(v["spec"])))
    total = sum(src_c.values()) or 1
    covered = sum(min(n, out_c.get(ch, 0)) for ch, n in src_c.items())
    cov = round(covered * 100 / total, 1)
    man = v["manifest"]
    man["coverage"] = cov
    man["rebuilt_from"] = fid
    if target == "doc" and cov < 98:
        man.setdefault("warnings", []).append(f"文字覆盖率 {cov}%：部分原文未出现在重建结果中，请核对")
    elif target == "ppt":
        man.setdefault("warnings", []).append(f"原文文字在演示文稿中的覆盖率为 {cov}%（演示文稿会精简表述，仅供参考）")
    db.update("versions", {"id": v["id"]}, {"manifest": man})
    db.update("works", {"id": res["work_id"]}, {"source": "rebuild", "origin_file_id": fid})
    res["coverage"] = cov
    return res
