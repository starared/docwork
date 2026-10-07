"""AI 生成 PPT：资料 → 大纲（可确认）→ 主题 → 页面规格 → 配图 → 渲染 → 检查与修正 → 版本。"""
from __future__ import annotations

import json
import re
import zipfile

from pydantic import ValidationError

from .. import jobs, models_cfg, storage
from ..render.theme import PRESET_THEMES, normalize_theme, preset_theme
from ..spec.common import Theme
from ..spec.deck import Deck, Slide, deck_text
from ..spec.ops import get_path, set_path
from ..util import UserError, estimate_tokens, new_id
from . import prompts
from .check import condense_ok
from .common import (add_source_images, collect_sources, ensure_work, file_asset, render, resolve_images, save_version,
                     source_block)
from .research import citation_hint
from .context import Ctx

AWAITING = object()
BATCH = 6


def handle_gen_ppt(ctx: Ctx):
    p = ctx.p
    prev = ctx.job.get("result") or {}
    # 1. 资料
    if prev.get("sources") is not None:
        sources, src_images = prev["sources"], prev.get("src_images", [])
        web_refs, web_notes = prev.get("web_refs", []), prev.get("web_notes", [])
    else:
        sources, src_images, web_refs, web_notes = collect_sources(ctx, 0.0, 0.15)
    ctx.web_refs = web_refs
    ctx.check()
    # 2. 大纲
    outline = p.get("outline")
    if not outline:
        ctx.progress(0.17, "规划大纲")
        outline = make_outline(ctx, sources)
        if p.get("confirm_outline"):
            jobs.set_awaiting(ctx.id, {"outline": outline, "sources": sources, "src_images": src_images,
                                       "web_refs": web_refs, "web_notes": web_notes}, "大纲已生成，请确认或修改后继续")
            return AWAITING
    title = outline.get("title") or p.get("topic", "")[:30] or "演示文稿"
    work_id = ensure_work(ctx, "ppt", title)
    ctx.check()
    # 3. 主题
    ctx.progress(0.25, "设计主题")
    theme = choose_theme(ctx, outline)
    # 4. 页面规格
    assets: dict = {}
    avail = add_source_images(src_images, assets)
    slides = make_slides(ctx, outline, theme, sources, avail, 0.3, 0.6)
    deck_d = {"title": title, "aspect": p.get("aspect", "16:9"), "theme": theme.model_dump(), "slides": slides,
              "footer": p.get("footer", ""), "language": p.get("language", "zh")}
    if p.get("logo_file_id"):
        deck_d["logo_asset"] = file_asset(p["logo_file_id"], assets)
    for rid in p.get("image_file_ids") or []:
        file_asset(rid, assets)
    if web_refs and deck_d["slides"]:
        # 网络资料的出处写在最后一页的演讲备注里
        last = deck_d["slides"][-1]
        last["notes"] = ((last.get("notes") or "") + "\n\n参考资料：\n"
                         + "\n".join(f"[{r['n']}] {r['title']} {r['url']}" for r in web_refs)).strip()
    deck = Deck.model_validate(deck_d)
    # 5. 配图
    ctx.progress(0.6, "配图")
    dd = deck.model_dump(mode="json")
    notes = web_notes + resolve_images(ctx, dd, assets, p.get("image_mode", "auto"), lo=0.6, hi=0.7)
    deck = Deck.model_validate(dd)
    # 6-8. 渲染、检查、修正
    deck, rendered, fix_log = render_and_fix(ctx, deck, assets, title, 0.7, 0.95)
    ctx.progress(0.96, "保存版本")
    v = save_version(ctx, work_id, "ppt", deck.model_dump(mode="json"), rendered, assets, source="generate",
                     message=p.get("topic", "")[:200], title=title, search_text=deck_text(deck),
                     extra={"warnings": notes + rendered.get("warnings", []), "fix_log": fix_log, "outline": outline})
    return {"work_id": work_id, "version_id": v["id"], "issues": len(unresolved(rendered)), "notes": notes}


# ---------- 大纲 ----------

def make_outline(ctx: Ctx, sources: str) -> dict:
    p = ctx.p
    req = [f"题目：{p.get('topic', '')}"]
    for k, label in (("pages", "页数"), ("audience", "受众"), ("purpose", "用途"), ("style", "风格偏好"), ("language", "语言"), ("extra", "其他要求")):
        if p.get(k):
            req.append(f"{label}：{p[k]}")
    user = "\n".join(req) + "\n\n" + source_block(sources)

    def validate(d):
        if not isinstance(d, dict) or not isinstance(d.get("slides"), list) or not d["slides"]:
            raise ValueError("需要 slides 数组")
        for s in d["slides"]:
            if not s.get("title") and s.get("intent") not in ("cover", "ending"):
                raise ValueError("每页都需要标题")
        n = int(p.get("pages") or 0)
        if n and abs(len(d["slides"]) - n) > 0:
            raise ValueError(f"页数应为 {n} 页，实际 {len(d['slides'])} 页")
        return d
    return ctx.llm.json("planner", prompts.PPT_OUTLINE, user, validate=validate, stream=True, max_tokens=12000)


# ---------- 主题 ----------

def choose_theme(ctx: Ctx, outline: dict) -> Theme:
    p = ctx.p
    font_mode = p.get("font_mode", "system")
    if p.get("theme_file_id"):
        t = theme_from_pptx(p["theme_file_id"], font_mode)
        if t:
            return t
    if p.get("theme_preset") in PRESET_THEMES:
        return preset_theme(p["theme_preset"], font_mode)
    user = f"题目：{p.get('topic', '')}\n标题：{outline.get('title', '')}\n风格偏好：{p.get('style', '无')}\n用途：{p.get('purpose', '')}"
    try:
        d = ctx.llm.json("fast", prompts.PPT_THEME, user, max_tokens=600)
        base = dict(PRESET_THEMES.get(d.get("preset"), PRESET_THEMES["商务蓝"]))
        for k in ("primary", "secondary", "accent", "background", "surface", "text", "muted", "decor", "cover"):
            if d.get(k):
                base[k] = d[k]
        base["name"] = d.get("preset", "自定义")
        base["font_mode"] = font_mode
        return normalize_theme(Theme.model_validate(base))
    except Exception:
        return preset_theme("商务蓝", font_mode)


def theme_from_pptx(file_id: str, font_mode: str) -> Theme | None:
    """只提取已有 PPT 的配色和字体作为主题参数，不使用它的母版。"""
    f = storage.get_file(file_id)
    if not f:
        return None
    try:
        with zipfile.ZipFile(storage.blob_path(f["sha"])) as z:
            name = next((n for n in z.namelist() if re.match(r"ppt/theme/theme\d+\.xml$", n)), None)
            if not name:
                return None
            xml = z.read(name).decode("utf-8", "replace")
    except (zipfile.BadZipFile, OSError):
        return None

    def color(tag):
        m = re.search(rf"<a:{tag}>\s*<a:(?:srgbClr val|sysClr[^>]*lastClr)=\"([0-9A-Fa-f]{{6}})\"", xml)
        return "#" + m.group(1).upper() if m else None

    base = dict(PRESET_THEMES["商务蓝"])
    for key, tag in (("primary", "accent1"), ("secondary", "accent2"), ("accent", "accent3"), ("text", "dk1"), ("background", "lt1")):
        c = color(tag)
        if c:
            base[key] = c
    major = re.search(r"<a:majorFont>.*?<a:latin typeface=\"([^\"]+)\".*?<a:ea typeface=\"([^\"]*)\"", xml, re.S)
    minor = re.search(r"<a:minorFont>.*?<a:latin typeface=\"([^\"]+)\".*?<a:ea typeface=\"([^\"]*)\"", xml, re.S)
    t = dict(base, name="来自上传文件", font_mode=font_mode)
    if major and major.group(2):
        t["heading_font"] = major.group(2)
    if minor:
        t["latin_font"] = minor.group(1) if not minor.group(1).startswith("+") else "Arial"
        if minor.group(2):
            t["body_font"] = minor.group(2)
    try:
        return normalize_theme(Theme.model_validate(t))
    except ValidationError:
        return None


# ---------- 页面规格 ----------

def make_slides(ctx: Ctx, outline: dict, theme: Theme, sources: str, avail: list[dict], lo: float, hi: float) -> list[dict]:
    items = outline["slides"]
    overview = "\n".join(f"{i + 1}. [{s.get('intent', '')}] {s.get('title', '')}" for i, s in enumerate(items))
    src = sources
    if estimate_tokens(src) > 8000:
        src = src[:12000] + "\n……（资料较长，已截断；各页所需数据见大纲中的 data 字段）"
    out: list[dict] = []
    nb = (len(items) + BATCH - 1) // BATCH
    for b in range(nb):
        ctx.check()
        part = items[b * BATCH:(b + 1) * BATCH]
        start = b * BATCH
        ctx.progress(lo + (hi - lo) * b / nb, "编写页面", f"第 {start + 1}–{start + len(part)} 页")
        lang = ctx.p.get("language") or "简体中文"
        user = (f"演示标题：{outline.get('title', '')}\n输出语言：{lang}（标题、正文、备注都用这种语言）\n全部页面：\n{overview}\n\n"
                f"本次需要编写第 {start + 1}–{start + len(part)} 页：\n{json.dumps(part, ensure_ascii=False, indent=1)}\n\n"
                f"主题：{theme.name}（深色背景：{'是' if theme.background.upper() in ('#0F172A', '#111827') else '否'}）\n"
                + (f"可用素材：{json.dumps(avail, ensure_ascii=False)}\n" if avail else "")
                + "\n" + source_block(src, citation_hint(getattr(ctx, "web_refs", []), "在该页的演讲备注（notes）中")))
        got = _slides_batch(ctx, user, part)
        for s, item in zip(got, part):
            # 页面标题以大纲为准：用户在确认大纲时可能改过，模型写页面时不应改写
            t = str(item.get("title") or "").strip()
            if t and len(t) <= 40 and s.get("layout") not in ("cover",):
                s["title"] = t
        out.extend(got)
    return out


def _slides_batch(ctx: Ctx, user: str, part: list[dict]) -> list[dict]:
    try:
        d = ctx.llm.json("planner", prompts.PPT_SLIDES, user, stream=True, max_tokens=8000,
                         validate=lambda d: d if isinstance(d, dict) and isinstance(d.get("slides"), list) else (_ for _ in ()).throw(ValueError("需要 slides 数组")))
        raw = d["slides"]
    except Exception:
        raw = []
    result: list[dict] = []
    bad: list[tuple[int, dict, str]] = []
    for i, item in enumerate(part):
        s = raw[i] if i < len(raw) else None
        if s is None:
            bad.append((i, {}, "缺少这一页"))
            result.append({})
            continue
        try:
            result.append(Slide.model_validate(s).model_dump(mode="json"))
        except ValidationError as e:
            bad.append((i, s, str(e)[:600]))
            result.append({})
    for attempt in range(2):
        if not bad:
            break
        fix_user = "以下页面不合格，请逐页修正后输出 {\"slides\": [...]}，顺序与列出的一致：\n" + "\n".join(
            f"- 大纲：{json.dumps(part[i], ensure_ascii=False)}\n  原输出：{json.dumps(s, ensure_ascii=False)[:1500]}\n  错误：{err}" for i, s, err in bad)
        try:
            d = ctx.llm.json("planner", prompts.PPT_SLIDES, fix_user, max_tokens=6000)
            fixed = d.get("slides", []) if isinstance(d, dict) else []
        except Exception:
            fixed = []
        still = []
        for k, (i, s, err) in enumerate(bad):
            cand = fixed[k] if k < len(fixed) else None
            try:
                result[i] = Slide.model_validate(cand).model_dump(mode="json")
            except (ValidationError, TypeError, ValueError) as e:
                still.append((i, cand or s, str(e)[:600]))
        bad = still
    for i, _, _ in bad:
        result[i] = fallback_slide(part[i])
    return result


def fallback_slide(item: dict) -> dict:
    """模型多次输出不合格时的兜底页：按大纲要点生成要点页（总是合格）。"""
    intent = item.get("intent")
    title = (item.get("title") or "")[:40]
    if intent == "cover":
        return Slide(layout="cover", title=title or "演示文稿", content={}).model_dump(mode="json")
    if intent == "ending":
        return Slide(layout="ending", title=title or "谢谢", content={}).model_dump(mode="json")
    if intent == "section":
        return Slide(layout="section", title=title, content={}).model_dump(mode="json")
    pts = [str(x)[:88] for x in (item.get("points") or [])][:7] or [title or "（待补充）"]
    return Slide(layout="bullets", title=title, content={"bullets": [{"text": t} for t in pts]}).model_dump(mode="json")


# ---------- 渲染、检查与修正 ----------

def unresolved(rendered: dict) -> list[dict]:
    return [i for i in rendered.get("issues", []) if i.get("status") not in ("fixed", "visual_ok")]


def render_and_fix(ctx: Ctx, deck: Deck, assets: dict, title: str, lo: float, hi: float, *, page_cache: dict | None = None,
                   only: list[str] | None = None, step: str = "render") -> tuple[Deck, dict, list[str]]:
    """渲染并最多修正两轮：先精简文字，再拆页。仍有问题时用视觉模型复查，或标记为需要人工确认。

    only：只修正这些页面（对话修改时限定在变化的页面）。
    """
    log: list[str] = []
    cache = dict(page_cache or {})
    ctx.progress(lo, "渲染与排版检查")
    rendered = render(ctx, "ppt", deck.model_dump(mode="json"), assets, f"{step}_0", title=title, page_cache=cache)
    for rnd in (1, 2):
        issues = [i for i in rendered["issues"] if i.get("element") and (only is None or i["slide"] in only)]
        if not issues:
            break
        ctx.check()
        ctx.progress(lo + (hi - lo) * (0.3 * rnd), "修正排版", f"第 {rnd} 轮，{len(issues)} 处")
        d = deck.model_dump(mode="json")
        changed = False
        if rnd == 1:
            changed = _condense(ctx, d, issues, log)
        else:
            changed = _split(ctx, d, issues, log) or _condense(ctx, d, issues, log, factor=0.7)
        if not changed:
            break
        deck = Deck.model_validate(d)
        for pg in rendered.get("pages", []):
            cache[pg["hash"]] = pg["preview"]
        rendered = render(ctx, "ppt", deck.model_dump(mode="json"), assets, f"{step}_{rnd}", title=title, page_cache=cache)
    # 剩余问题：视觉复查或标记人工确认
    rest = [i for i in rendered["issues"] if only is None or i.get("slide") in only]
    if rest:
        _vision_review(ctx, rendered, rest, log)
    return deck, rendered, log


def _leaves(obj, prefix: str):
    if isinstance(obj, str):
        yield prefix, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("icon", "image_side", "asset", "query", "prompt", "source", "type", "number_format", "id", "layout"):
                continue
            yield from _leaves(v, f"{prefix}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _leaves(v, f"{prefix}.{i}")


def _condense(ctx: Ctx, d: dict, issues: list[dict], log: list[str], factor: float = 0.75) -> bool:
    by_id = {s["id"]: s for s in d["slides"]}
    items = []
    for i in issues:
        s = by_id.get(i["slide"])
        if not s or not i.get("element") or i["element"].startswith("_"):
            continue
        try:
            node = get_path(s, i["element"])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        for path, text in _leaves(node, i["element"]):
            if len(text) >= 8:
                items.append({"id": f"{s['id']}|{path}", "text": text, "target": max(6, int(len(text) * factor))})
    if not items:
        return False
    user = "请精简以下文字（target 为目标字数）：\n" + json.dumps(items, ensure_ascii=False)
    try:
        res = ctx.llm.json("writer", prompts.CONDENSE, user, max_tokens=4000)
    except Exception:
        return False
    orig = {x["id"]: x["text"] for x in items}
    changed = False
    for it in (res.get("items") or []) if isinstance(res, dict) else []:
        k, new = it.get("id"), (it.get("text") or "").strip()
        if k not in orig or not new:
            continue
        ok, why = condense_ok(orig[k], new)
        sid, _, path = k.partition("|")
        if not ok:
            log.append(f"未采用对“{orig[k][:16]}…”的精简：{why}")
            continue
        try:
            set_path(by_id[sid], path, new)
            changed = True
        except (KeyError, IndexError, ValueError):
            continue
    if changed:
        log.append(f"精简了 {len(items)} 处文字")
    return changed


SPLITTABLE = {"bullets", "table", "two_column", "comparison", "cards", "timeline", "process"}


def _split(ctx: Ctx, d: dict, issues: list[dict], log: list[str]) -> bool:
    sids = list(dict.fromkeys(i["slide"] for i in issues))
    changed = False
    for sid in sids:
        idx = next((k for k, s in enumerate(d["slides"]) if s["id"] == sid), None)
        if idx is None or d["slides"][idx]["layout"] not in SPLITTABLE:
            continue
        s = d["slides"][idx]
        new = _split_local(s)
        if new is None:
            try:
                res = ctx.llm.json("planner", prompts.PPT_SPLIT + "\n\n" + prompts.PPT_SLIDES, json.dumps(s, ensure_ascii=False), max_tokens=4000)
                cand = res.get("slides") if isinstance(res, dict) else None
                if isinstance(cand, list) and len(cand) == 2:
                    new = [Slide.model_validate(c).model_dump(mode="json") for c in cand]
            except Exception:
                new = None
        if new:
            new[0]["id"] = s["id"]
            new[1]["id"] = new_id("s")
            d["slides"][idx:idx + 1] = new
            changed = True
            log.append(f"第 {idx + 1} 页内容过多，已拆成两页")
    return changed


def _split_local(s: dict) -> list[dict] | None:
    """要点页和表格页直接按条目对半拆分，不需要模型。"""
    c = s["content"]
    if s["layout"] == "bullets" and len(c["bullets"]) >= 2:
        h = (len(c["bullets"]) + 1) // 2
        a = dict(s, content={"bullets": c["bullets"][:h]})
        b = dict(s, title=_cont(s["title"]), content={"bullets": c["bullets"][h:]}, notes="")
        return [Slide.model_validate(a).model_dump(mode="json"), Slide.model_validate(b).model_dump(mode="json")]
    if s["layout"] == "table" and len(c["table"]["rows"]) >= 4:
        rows = c["table"]["rows"]
        h = (len(rows) + 1) // 2
        a = dict(s, content={"table": dict(c["table"], rows=rows[:h])})
        b = dict(s, title=_cont(s["title"]), content={"table": dict(c["table"], rows=rows[h:])}, notes="")
        return [Slide.model_validate(a).model_dump(mode="json"), Slide.model_validate(b).model_dump(mode="json")]
    return None


def _cont(t: str) -> str:
    if t and not any("\u4e00" <= ch <= "\u9fff" for ch in t):
        return t[:33] + " (cont.)"
    return (t[:36] + "（续）") if t else "（续）"


def _vision_review(ctx: Ctx, rendered: dict, rest: list[dict], log: list[str]) -> None:
    has_vision = models_cfg.status()["vision"]
    pages = {p["id"]: p for p in rendered.get("pages", [])}
    reviewed: dict[str, dict] = {}
    for i in rest:
        sid = i.get("slide")
        if not has_vision or not sid or sid not in pages:
            i["status"] = "needs_review"
            continue
        if sid not in reviewed:
            try:
                img = storage.read_bytes(pages[sid]["preview"])
                ans = ctx.llm.vision(prompts.VISION_CHECK, [img], max_tokens=300)
                from ..llm import extract_json
                reviewed[sid] = extract_json(ans)
            except Exception:
                reviewed[sid] = {"ok": None}
        ok = reviewed[sid].get("ok")
        i["status"] = "visual_ok" if ok is True else ("needs_review" if ok is None else "unresolved")
        if ok is False:
            i["vision"] = reviewed[sid].get("problems", [])
    n_bad = sum(1 for i in rest if i.get("status") in ("unresolved", "needs_review"))
    if n_bad:
        log.append(f"仍有 {n_bad} 处排版问题需要确认")
