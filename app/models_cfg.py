"""模型接口配置。

- 接口（endpoints）只保存连接信息：OpenAI 兼容接口（地址 + API Key）或图库（Pexels / Unsplash）。
- 模型（models）从接口的 /models 拉取后由管理员挑选添加，也可以手动填写模型名；一个接口下可以有任意多个模型。
- 接口和模型都不按“文字/视觉/生图”分类。每个角色（规划、写作、快速、视觉、图像生成）由管理员指定具体模型，
  同一个模型可以同时负责多个角色（例如既能写作又能看图的多模态模型）。
"""
from __future__ import annotations

from . import db, security
from .util import UserError, new_id, now

KINDS = {"openai": "OpenAI 兼容接口", "stock": "图库"}
ROLES = {
    "planner": ("规划", "大纲、页面规格、修改操作，要求推理强、能稳定输出 JSON"),
    "writer": ("写作", "正文、演讲备注、改写、翻译"),
    "fast": ("快速", "资料分块摘要、标题、分类，要求便宜、快"),
}
# 使用模型的全部角色（图库角色指向的是接口，不是模型）
MODEL_ROLES = {**{k: v[0] for k, v in ROLES.items()}, "vision": "视觉", "image": "图像生成"}
ROLE_FALLBACK = {"planner": ["planner", "writer", "fast"], "writer": ["writer", "planner", "fast"], "fast": ["fast", "writer", "planner"]}


# ---------- 接口 ----------

def _endpoint_row(r: dict, include_key: bool) -> dict:
    r["extra"] = db.jload(r["extra"], {})
    r["capabilities"] = db.jload(r["capabilities"], {})
    r["available"] = db.jload(r.get("available"), [])
    r["has_key"] = bool(r["api_key_enc"])
    if include_key:
        r["api_key"] = security.decrypt_secret(r["api_key_enc"])
    r.pop("api_key_enc", None)
    return r


def list_endpoints() -> list[dict]:
    return [_endpoint_row(r, False) for r in db.all_("SELECT * FROM endpoints ORDER BY created_at")]


def get_endpoint(eid: str, with_key: bool = True) -> dict | None:
    r = db.one("SELECT * FROM endpoints WHERE id=?", (eid,))
    return _endpoint_row(r, with_key) if r else None


def save_endpoint(data: dict, eid: str | None = None) -> str:
    old = None
    if eid:
        old = db.one("SELECT kind FROM endpoints WHERE id=?", (eid,))
        if not old:
            raise UserError("接口不存在", 404)
    kind = old["kind"] if old else (data.get("kind") or "openai")
    if kind not in KINDS:
        raise UserError("接口类型无效")
    base = (data.get("base_url") or "").strip().rstrip("/")
    extra = data.get("extra") or {}
    if kind == "stock":
        provider = extra.get("provider", "pexels")
        if provider not in ("pexels", "unsplash"):
            raise UserError("图库只支持 pexels 或 unsplash")
        base = base or ("https://api.pexels.com" if provider == "pexels" else "https://api.unsplash.com")
    if not base.startswith(("http://", "https://")):
        raise UserError("接口地址必须以 http:// 或 https:// 开头")
    row = {
        "name": (data.get("name") or KINDS[kind]).strip()[:60],
        "kind": kind,
        "base_url": base,
        "extra": extra,
        "enabled": 1 if data.get("enabled", True) else 0,
    }
    if data.get("api_key"):
        row["api_key_enc"] = security.encrypt_secret(data["api_key"].strip())
    if eid:
        db.update("endpoints", {"id": eid}, row)
        return eid
    row.update({"id": new_id("e_"), "created_at": now(), "capabilities": {}, "model": "", "available": []})
    row.setdefault("api_key_enc", "")
    db.insert("endpoints", row)
    return row["id"]


def delete_endpoint(eid: str) -> None:
    mids = {m["id"] for m in db.all_("SELECT id FROM models WHERE endpoint_id=?", (eid,))}
    db.run("DELETE FROM models WHERE endpoint_id=?", (eid,))
    db.run("DELETE FROM endpoints WHERE id=?", (eid,))
    _drop_from_roles(mids | {eid})


def set_available(eid: str, names: list[str]) -> None:
    """保存从接口拉取到的模型列表（供挑选添加）。"""
    db.update("endpoints", {"id": eid}, {"available": sorted(set(names)), "available_at": now()})


# ---------- 模型 ----------

def _model_row(r: dict) -> dict:
    r["extra"] = db.jload(r["extra"], {})
    r["capabilities"] = db.jload(r["capabilities"], {})
    return r


def list_models() -> list[dict]:
    return [_model_row(r) for r in db.all_(
        "SELECT m.*, e.name AS endpoint_name FROM models m JOIN endpoints e ON e.id=m.endpoint_id ORDER BY e.created_at, m.created_at")]


def get_model(mid: str, with_key: bool = True) -> dict | None:
    """返回模型及其所属接口的连接信息，可以直接交给 LLM 调用。"""
    m = db.one("SELECT * FROM models WHERE id=?", (mid,))
    if not m:
        return None
    ep = get_endpoint(m["endpoint_id"], with_key=with_key)
    if not ep:
        return None
    m = _model_row(m)
    m.update({
        "base_url": ep["base_url"],
        "endpoint_name": ep["name"],
        "extra": {**ep["extra"], **m["extra"]},
        "enabled": 1 if (m["enabled"] and ep["enabled"]) else 0,
    })
    if with_key:
        m["api_key"] = ep.get("api_key", "")
    return m


def add_models(eid: str, names: list[str]) -> list[str]:
    ep = db.one("SELECT kind FROM endpoints WHERE id=?", (eid,))
    if not ep:
        raise UserError("接口不存在", 404)
    if ep["kind"] != "openai":
        raise UserError("图库接口没有模型")
    out = []
    for n in names:
        n = str(n or "").strip()[:200]
        if not n:
            continue
        r = db.one("SELECT id FROM models WHERE endpoint_id=? AND model=?", (eid, n))
        if r:
            out.append(r["id"])
            continue
        mid = new_id("m_")
        db.insert("models", {"id": mid, "endpoint_id": eid, "model": n, "name": n[:60], "extra": {}, "capabilities": {},
                             "enabled": 1, "created_at": now()})
        out.append(mid)
    return out


def update_model(mid: str, data: dict) -> None:
    if not db.one("SELECT id FROM models WHERE id=?", (mid,)):
        raise UserError("模型不存在", 404)
    row: dict = {}
    if "name" in data:
        row["name"] = str(data["name"] or "").strip()[:60]
    for k in ("price_in", "price_out", "price_image"):
        if k in data:
            row[k] = float(data[k] or 0)
    if "enabled" in data:
        row["enabled"] = 1 if data["enabled"] else 0
    if "extra" in data and isinstance(data["extra"], dict):
        row["extra"] = data["extra"]
    if row:
        db.update("models", {"id": mid}, row)


def delete_model(mid: str) -> None:
    db.run("DELETE FROM models WHERE id=?", (mid,))
    _drop_from_roles({mid})


# ---------- 角色 ----------

def _drop_from_roles(ids: set[str]) -> None:
    roles = get_roles()
    changed = False
    for k, v in list(roles.items()):
        if v in ids:
            roles[k] = None
            changed = True
    if changed:
        db.set_setting("roles", roles)


def get_roles() -> dict:
    roles = dict(db.get_setting("roles", {}) or {})
    # v3 之前模型角色指向接口；迁移后对应模型的 ID 是 "m_" + 接口 ID
    for k in MODEL_ROLES:
        v = roles.get(k)
        if v and v.startswith("e_") and db.one("SELECT id FROM models WHERE id=?", ("m_" + v,)):
            roles[k] = "m_" + v
    return roles


def set_roles(roles: dict) -> None:
    out = {}
    for k in MODEL_ROLES:
        v = roles.get(k)
        if v and not db.one("SELECT id FROM models WHERE id=?", (v,)):
            raise UserError(f"{MODEL_ROLES[k]}角色指定的模型不存在")
        out[k] = v or None
    v = roles.get("stock")
    if v and not db.one("SELECT id FROM endpoints WHERE id=? AND kind='stock'", (v,)):
        raise UserError("图库角色需要指定图库接口")
    out["stock"] = v or None
    db.set_setting("roles", out)


def endpoint_for(role: str) -> dict | None:
    """按角色取模型（图库角色取接口）。

    只使用管理员指定的模型，不按类型猜测：文字三个角色之间互相回退；
    视觉角色未指定时，如果规划模型测试过能看图，就由它兼任。
    """
    roles = get_roles()
    if role == "stock":
        eid = roles.get("stock")
        ep = get_endpoint(eid) if eid else None
        if not ep:
            ep = next((get_endpoint(e["id"]) for e in list_endpoints() if e["kind"] == "stock" and e["enabled"]), None)
        return ep if ep and ep["enabled"] else None
    for r in ROLE_FALLBACK.get(role, [role]):
        mid = roles.get(r)
        if mid:
            m = get_model(mid)
            if m and m["enabled"]:
                return m
    if role == "vision":
        m = endpoint_for("planner")
        if m and (m.get("capabilities") or {}).get("vision") is True:
            return m
    return None


def capability(role: str, cap: str) -> bool:
    ep = endpoint_for(role)
    if not ep:
        return False
    caps = ep.get("capabilities") or {}
    return caps.get(cap, True) is not False


def status() -> dict:
    """各项功能在当前角色分配下是否可用（用于界面显示与降级）。"""
    text = endpoint_for("planner")
    vision = endpoint_for("vision")
    image = endpoint_for("image")
    stock = endpoint_for("stock")
    tcaps = (text or {}).get("capabilities") or {}
    return {
        "text": bool(text) and tcaps.get("ok", True) is not False,
        "json": bool(text) and tcaps.get("json", True) is not False,
        "vision": bool(vision) and ((vision.get("capabilities") or {}).get("vision", True) is not False),
        "image": bool(image) and ((image.get("capabilities") or {}).get("image", True) is not False),
        "stock": bool(stock) and ((stock.get("capabilities") or {}).get("ok", True) is not False),
    }
