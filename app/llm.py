"""OpenAI 兼容接口客户端：对话（流式）、JSON 输出与校验修复、视觉输入、图像生成、图库检索。

- 优先使用 response_format=json_object；接口不支持时退回提示词约束 + 解析 + 校验，不合格自动修复重试。
- 超时、429、5xx 按指数退避重试；取消时断开连接（上游是否停止计费取决于接口）。
- 每次调用记录 token 用量（接口不返回时估算），调用前检查令牌配额。
"""
from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Callable

import httpx
from pydantic import BaseModel, ValidationError

from . import db, models_cfg, quota
from .util import UserError, estimate_tokens, new_id, now


class LLMError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


def _url(base: str, path: str) -> str:
    base = base.rstrip("/")
    if base.endswith(path):
        return base
    if path == "/chat/completions" and base.endswith("/chat/completions"):
        return base
    return base + path


IMAGE_MAX = 15 * 1024 * 1024           # 单张图片上限
IMAGE_RESPONSE_MAX = 30 * 1024 * 1024  # 图像接口响应上限（base64 约为图片的 4/3）


def _read_limited(r: "httpx.Response", max_bytes: int) -> bytes:
    buf = bytearray()
    for chunk in r.iter_bytes():
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise LLMError("接口响应过大，已中止")
    return bytes(buf)


def _client(timeout_read: float = 300) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(connect=15, read=timeout_read, write=60, pool=15), follow_redirects=True)


def extract_json(text: str) -> Any:
    """从模型输出中取出 JSON：去掉代码块标记，找到第一个完整的对象或数组。"""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S).strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    start = min([i for i in (s.find("{"), s.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("输出中没有 JSON")
    stack, in_str, esc = [], False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            stack.pop()
            if not stack:
                return json.loads(s[start:i + 1])
    raise ValueError("JSON 不完整（输出可能被截断）")


class LLM:
    """绑定到一个任务的模型调用器。"""

    def __init__(self, job: dict | None = None, check_cancel: Callable[[], None] | None = None,
                 on_stream: Callable[[str], None] | None = None, override: dict | None = None):
        self.override = override
        self.job = job or {}
        self.root_id = (self.job.get("params") or {}).get("_root") or self.job.get("id")
        self.check_cancel = check_cancel or (lambda: None)
        self.on_stream = on_stream

    def _ep(self, role: str) -> dict | None:
        return self.override or models_cfg.endpoint_for(role)

    # ---------- 用量 ----------

    def _record(self, ep: dict, role: str, pt: int, ct: int, estimated: bool, images: int = 0):
        cost = pt / 1e6 * (ep.get("price_in") or 0) + ct / 1e6 * (ep.get("price_out") or 0) + images * (ep.get("price_image") or 0)
        db.insert("usage", {
            "id": new_id("u_"), "job_id": self.job.get("id"), "root_job_id": self.root_id,
            "workspace_id": self.job.get("workspace_id"), "token_id": self.job.get("token_id"),
            "endpoint_id": ep.get("endpoint_id") or ep["id"],
            "model": ep.get("model", ""), "role": role, "prompt_tokens": int(pt), "completion_tokens": int(ct),
            "images": images, "estimated": 1 if estimated else 0, "cost": round(cost, 6), "created_at": now(),
        })

    # ---------- 对话 ----------

    def chat(self, role: str, messages: list[dict], *, json_mode: bool = False, stream: bool = False,
             max_tokens: int | None = None, temperature: float = 0.4, timeout: float = 300) -> str:
        ep = self._ep(role)
        if not ep:
            raise UserError("尚未给这项工作指定模型，请管理员在后台“模型接口”的角色分配中选择", 503, "no_model")
        est_in = sum(estimate_tokens(m["content"] if isinstance(m["content"], str) else json.dumps(m["content"], ensure_ascii=False)) for m in messages)
        quota.check_during(self.root_id or "", self.job.get("token_id"), est_in + (max_tokens or 2000))
        caps = ep.get("capabilities") or {}
        body: dict = {"model": ep["model"], "messages": messages, "temperature": temperature}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if json_mode and caps.get("json_mode", True) is not False:
            body["response_format"] = {"type": "json_object"}
        extra = (ep.get("extra") or {}).get("body") or {}
        body.update(extra)
        headers = {"Authorization": f"Bearer {ep.get('api_key', '')}", "Content-Type": "application/json"}
        headers.update((ep.get("extra") or {}).get("headers") or {})
        url = _url(ep["base_url"], "/chat/completions")
        last_err = None
        for attempt in range(4):
            self.check_cancel()
            try:
                if stream:
                    text, usage = self._stream(url, headers, dict(body, stream=True, stream_options={"include_usage": True}), timeout)
                else:
                    text, usage = self._once(url, headers, body, timeout)
                if usage:
                    self._record(ep, role, usage.get("prompt_tokens", est_in), usage.get("completion_tokens", estimate_tokens(text)), False)
                else:
                    self._record(ep, role, est_in, estimate_tokens(text), True)
                return text
            except _Retry as e:
                last_err = str(e)
                if e.drop_json and "response_format" in body:
                    body.pop("response_format", None)
                    _set_cap(ep, "json_mode", False)
                    continue
                if e.drop_stream_opts:
                    stream_opts_ok = False
                    body.pop("stream_options", None)
                    if stream:
                        try:
                            text, usage = self._stream(url, headers, dict(body, stream=True), timeout)
                            self._record(ep, role, est_in, estimate_tokens(text), True)
                            return text
                        except _Retry as e2:
                            last_err = str(e2)
                    _ = stream_opts_ok
                time.sleep(min(30, 2 ** (attempt + 1)))
        raise LLMError(f"模型接口调用失败：{last_err}")

    def _once(self, url, headers, body, timeout):
        try:
            with _client(timeout) as c:
                r = c.post(url, headers=headers, json=body)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise _Retry(f"连接失败或超时：{type(e).__name__}")
        self._check_status(r)
        try:
            data = r.json()
            text = data["choices"][0]["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise _Retry(f"接口返回格式无法识别：{r.text[:200]}")
        return text, data.get("usage")

    def _stream(self, url, headers, body, timeout):
        parts: list[str] = []
        usage = None
        last_emit = 0.0
        try:
            with _client(timeout) as c, c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code >= 400:
                    r.read()
                    self._check_status(r)
                for line in r.iter_lines():
                    self.check_cancel()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for ch in chunk.get("choices") or []:
                        delta = (ch.get("delta") or {}).get("content")
                        if delta:
                            parts.append(delta)
                    if self.on_stream and time.time() - last_emit > 0.4:
                        self.on_stream("".join(parts))
                        last_emit = time.time()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise _Retry(f"连接失败或超时：{type(e).__name__}")
        text = "".join(parts)
        if self.on_stream:
            self.on_stream(text)
        return text, usage

    def _check_status(self, r: httpx.Response):
        if r.status_code < 400:
            return
        txt = r.text[:500]
        if r.status_code == 400 and "response_format" in txt:
            raise _Retry("接口不支持 JSON 模式", drop_json=True)
        if r.status_code == 400 and "stream_options" in txt:
            raise _Retry("接口不支持 stream_options", drop_stream_opts=True)
        if r.status_code in (401, 403):
            raise LLMError(f"模型接口认证失败（{r.status_code}）：请检查 API Key")
        if r.status_code == 404:
            raise LLMError(f"模型接口地址或模型名不存在（404）：{txt[:200]}")
        if r.status_code == 429 or r.status_code >= 500:
            raise _Retry(f"接口繁忙或出错（{r.status_code}）：{txt[:200]}")
        raise LLMError(f"模型接口返回错误（{r.status_code}）：{txt}")

    # ---------- JSON ----------

    def json(self, role: str, system: str, user: str | list, *, model: type[BaseModel] | None = None,
             validate: Callable[[Any], Any] | None = None, stream: bool = False, max_tokens: int | None = None,
             retries: int = 2, temperature: float = 0.3) -> Any:
        """要求模型输出 JSON 并校验；不合格时把错误反馈给模型修复。"""
        messages = [{"role": "system", "content": system + "\n\n只输出一个 JSON 对象，不要输出解释或代码块标记。"},
                    {"role": "user", "content": user}]
        last = ""
        for attempt in range(retries + 1):
            text = self.chat(role, messages, json_mode=True, stream=stream, max_tokens=max_tokens, temperature=temperature)
            try:
                data = extract_json(text)
                if model is not None:
                    data = model.model_validate(data)
                if validate is not None:
                    data = validate(data)
                return data
            except (ValueError, ValidationError) as e:
                last = _brief_error(e)
                messages = messages[:2] + [
                    {"role": "assistant", "content": text[-6000:]},
                    {"role": "user", "content": f"上面的输出不合格：{last}\n请修正后重新输出完整的 JSON。"},
                ]
        raise LLMError(f"模型多次输出不合格的结果：{last}")

    # ---------- 视觉 ----------

    def vision(self, prompt: str, images: list[bytes], max_tokens: int = 800) -> str:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for b in images:
            content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b).decode()}})
        return self.chat("vision", [{"role": "user", "content": content}], max_tokens=max_tokens, temperature=0)

    # ---------- 图像生成 ----------

    def generate_image(self, prompt: str, size: str = "1792x1024") -> bytes:
        ep = self._ep("image")
        if not ep:
            raise UserError("尚未指定图像生成模型", 503, "no_model")
        self.check_cancel()
        if (ep.get("capabilities") or {}).get("image_api") == "chat":
            return self._image_via_chat(ep, prompt, size)
        try:
            return self._image_via_images(ep, prompt, size)
        except LLMError as e:
            # 有些图像模型（例如经 new-api 转发的 Gemini 图像模型）只能通过对话接口出图：
            # 改用对话接口，成功后记到模型上，以后直接走对话接口
            try:
                data = self._image_via_chat(ep, prompt, size)
            except LLMError:
                raise e
            _set_cap(ep, "image_api", "chat")
            return data

    def _image_via_images(self, ep: dict, prompt: str, size: str) -> bytes:
        body = {"model": ep["model"], "prompt": prompt, "n": 1, "size": (ep.get("extra") or {}).get("size", size)}
        if (ep.get("extra") or {}).get("b64", True):
            body["response_format"] = "b64_json"
        headers = {"Authorization": f"Bearer {ep.get('api_key', '')}"}
        url = _url(ep["base_url"], "/images/generations")
        for attempt in range(3):
            try:
                with _client(180) as c:
                    # 响应体按上限流式读取：接口异常或被篡改时不能一次把超大响应读进内存
                    with c.stream("POST", url, headers=headers, json=body) as r:
                        raw = _read_limited(r, IMAGE_RESPONSE_MAX)
                    text = raw.decode("utf-8", "replace")
                    if r.status_code == 400 and "response_format" in text:
                        body.pop("response_format", None)
                        continue
                    if r.status_code >= 400:
                        # 接口明确表示没有这个模型（例如 new-api 只给它配了对话接口）时不必重试
                        unavailable = "model_not_found" in text or "No available channel" in text or "only supported" in text
                        if (r.status_code == 429 or r.status_code >= 500) and not unavailable:
                            time.sleep(2 ** (attempt + 1))
                            continue
                        raise LLMError(f"图像生成失败（{r.status_code}）：{text[:200]}")
                    d = json.loads(text)["data"][0]
                    if d.get("b64_json"):
                        data = base64.b64decode(d["b64_json"])
                    else:
                        data = self.download(d["url"], max_bytes=IMAGE_MAX)
                    if len(data) > IMAGE_MAX:
                        raise LLMError("生成的图片过大")
                    self._record(ep, "image", estimate_tokens(prompt), 0, True, images=1)
                    return data
            except (httpx.TimeoutException, httpx.TransportError):
                time.sleep(2 ** (attempt + 1))
        raise LLMError("图像生成接口多次失败")

    def _image_via_chat(self, ep: dict, prompt: str, size: str) -> bytes:
        """通过对话接口出图：图片在回复的 images 字段、多段内容或 Markdown 图片里。"""
        try:
            w, h = (int(x) for x in str(size).lower().split("x", 1))
        except ValueError:
            w = h = 1
        shape = "wide landscape (16:9)" if w > h * 1.2 else ("tall portrait (9:16)" if h > w * 1.2 else "square (1:1)")
        body = {"model": ep["model"], "messages": [{"role": "user", "content":
                f"Generate an image: {prompt}\nAspect ratio: {shape}. Do not put any text, captions or watermarks in the image."}]}
        headers = {"Authorization": f"Bearer {ep.get('api_key', '')}", "Content-Type": "application/json"}
        url = _url(ep["base_url"], "/chat/completions")
        last = "图像生成接口多次失败"
        for attempt in range(3):
            self.check_cancel()
            try:
                with _client(240) as c:
                    with c.stream("POST", url, headers=headers, json=body) as r:
                        raw = _read_limited(r, IMAGE_RESPONSE_MAX)
                text = raw.decode("utf-8", "replace")
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"图像生成失败（{r.status_code}）：{text[:200]}"
                    time.sleep(2 ** (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise LLMError(f"图像生成失败（{r.status_code}）：{text[:200]}")
                d = json.loads(text)
                data = self._image_from_message(((d.get("choices") or [{}])[0]).get("message") or {})
                if not data:
                    raise LLMError("模型没有返回图片")
                if len(data) > IMAGE_MAX:
                    raise LLMError("生成的图片过大")
                usage = d.get("usage") or {}
                self._record(ep, "image", usage.get("prompt_tokens") or estimate_tokens(prompt),
                             usage.get("completion_tokens") or 0, not usage, images=1)
                return data
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = f"图像生成接口连接失败：{type(e).__name__}"
                time.sleep(2 ** (attempt + 1))
            except (ValueError, KeyError, IndexError, AttributeError):
                raise LLMError("图像生成接口返回的内容无法解析")
        raise LLMError(last)

    def _image_from_message(self, msg: dict) -> bytes | None:
        urls: list[str] = []
        for im in msg.get("images") or []:
            if isinstance(im, dict):
                u = (im.get("image_url") or {}).get("url") if isinstance(im.get("image_url"), dict) else im.get("image_url")
                urls.append(u or im.get("url") or "")
            elif isinstance(im, str):
                urls.append(im)
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    iu = part.get("image_url")
                    urls.append(iu.get("url", "") if isinstance(iu, dict) else str(iu or ""))
        elif isinstance(content, str):
            urls += re.findall(r"!\[[^\]]*\]\(\s*(data:image/[^)\s]+|https?://[^)\s]+)", content)
        for u in urls:
            if u.startswith("data:image/") and ";base64," in u:
                return base64.b64decode(u.split(";base64,", 1)[1])
            if u.startswith(("http://", "https://")):
                return self.download(u, max_bytes=IMAGE_MAX)
        return None

    # ---------- 图库 ----------

    def stock_search(self, query: str, orientation: str = "landscape", n: int = 5) -> list[dict]:
        ep = self._ep("stock")
        if not ep:
            return []
        provider = (ep.get("extra") or {}).get("provider", "pexels")
        try:
            with _client(30) as c:
                if provider == "pexels":
                    r = c.get(_url(ep["base_url"], "/v1/search"), params={"query": query, "per_page": n, "orientation": orientation},
                              headers={"Authorization": ep.get("api_key", "")})
                    r.raise_for_status()
                    return [{"url": p["src"].get("large2x") or p["src"]["original"], "credit": f"Pexels / {p.get('photographer', '')}",
                             "page": p.get("url", ""), "w": p.get("width"), "h": p.get("height")} for p in r.json().get("photos", [])]
                r = c.get(_url(ep["base_url"], "/search/photos"), params={"query": query, "per_page": n, "orientation": orientation},
                          headers={"Authorization": f"Client-ID {ep.get('api_key', '')}"})
                r.raise_for_status()
                return [{"url": p["urls"]["regular"], "credit": f"Unsplash / {p['user'].get('name', '')}",
                         "page": p["links"].get("html", ""), "w": p.get("width"), "h": p.get("height")} for p in r.json().get("results", [])]
        except (httpx.HTTPError, KeyError, ValueError):
            return []

    def download(self, url: str, max_bytes: int = IMAGE_MAX) -> bytes:
        if not str(url).lower().startswith(("https://", "http://")):
            raise LLMError("图片地址无效")
        with _client(60) as c, c.stream("GET", url) as r:
            r.raise_for_status()
            try:
                if int(r.headers.get("content-length") or 0) > max_bytes:
                    raise LLMError("图片过大")
            except ValueError:
                pass
            return _read_limited(r, max_bytes)


class _Retry(Exception):
    def __init__(self, msg: str, drop_json: bool = False, drop_stream_opts: bool = False):
        super().__init__(msg)
        self.drop_json = drop_json
        self.drop_stream_opts = drop_stream_opts


def _set_cap(ep: dict, key: str, value) -> None:
    """把调用中发现的能力（例如不支持 JSON 模式）记到模型上，下次不再尝试。"""
    table = "models" if ep.get("endpoint_id") else "endpoints"
    r = db.one(f"SELECT capabilities FROM {table} WHERE id=?", (ep["id"],))
    if not r:
        return
    caps = db.jload(r["capabilities"], {})
    caps[key] = value
    db.update(table, {"id": ep["id"]}, {"capabilities": caps})


def _brief_error(e: Exception) -> str:
    if isinstance(e, ValidationError):
        parts = []
        for err in e.errors()[:6]:
            loc = ".".join(str(x) for x in err.get("loc", ()))
            parts.append(f"{loc}: {err.get('msg', '')}")
        return "；".join(parts)
    return str(e)[:600]


def list_remote_models(eid: str) -> dict:
    """从 OpenAI 兼容接口的 /models 拉取模型列表，保存到接口上供挑选。"""
    ep = models_cfg.get_endpoint(eid)
    if not ep:
        raise UserError("接口不存在", 404)
    if ep["kind"] != "openai":
        raise UserError("图库接口没有模型列表")
    headers = {"Authorization": f"Bearer {ep.get('api_key', '')}"}
    headers.update((ep.get("extra") or {}).get("headers") or {})
    try:
        with _client(30) as c:
            r = c.get(_url(ep["base_url"], "/models"), headers=headers)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise LLMError(f"连接接口失败：{type(e).__name__}")
    if r.status_code in (401, 403):
        raise LLMError(f"接口认证失败（{r.status_code}）：请检查 API Key")
    if r.status_code >= 400:
        raise LLMError(f"接口没有返回模型列表（{r.status_code}）：{r.text[:200]}。可以在“添加模型”里手动填写模型名")
    try:
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        if items is None and isinstance(data, dict):
            items = data.get("models")
        names = [str(x.get("id") or x.get("name") or "") if isinstance(x, dict) else str(x) for x in items or []]
    except (ValueError, AttributeError, TypeError):
        raise LLMError(f"模型列表格式无法识别：{r.text[:200]}")
    names = [n for n in names if n][:2000]
    models_cfg.set_available(eid, names)
    return {"count": len(names)}


def test_model(mid: str, what: str = "chat") -> dict:
    """后台“测试”模型：chat 测连通性、JSON 输出、看图；image 测图像生成。只记录结果，不限制角色分配。"""
    import io

    ep = models_cfg.get_model(mid)
    if not ep:
        raise UserError("模型不存在", 404)
    caps: dict = {"tested_at": now()}
    llm = LLM({"id": None}, override=ep)
    t0 = time.time()
    if what == "image":
        try:
            data = llm.generate_image("a simple blue circle on white background, flat illustration", size="1024x1024")
            caps["image"] = len(data) > 1000
        except Exception as e:
            caps["image"] = False
            caps["image_error"] = str(e)[:300]
    else:
        try:
            txt = llm.chat("planner", [{"role": "user", "content": "只回复两个字：你好"}], max_tokens=20)
            caps["ok"] = bool(txt.strip())
            caps["latency_ms"] = int((time.time() - t0) * 1000)
            caps.pop("error", None)
        except Exception as e:
            caps["ok"] = False
            caps["error"] = str(e)[:300]
        if caps["ok"]:
            try:
                data = llm.json("planner", "你是测试助手。", '输出 {"a": 1, "b": "测试"}', max_tokens=60, retries=1)
                caps["json"] = isinstance(data, dict) and data.get("a") == 1
            except Exception as e:
                caps["json"] = False
                caps["json_error"] = str(e)[:200]
            try:
                from PIL import Image
                im = Image.new("RGB", (64, 64), (220, 30, 30))
                buf = io.BytesIO()
                im.save(buf, "PNG")
                ans = llm.vision("这张图片主要是什么颜色？只回答颜色。", [buf.getvalue()], max_tokens=20)
                caps["vision"] = "红" in ans or "red" in ans.lower()
            except Exception as e:
                caps["vision"] = False
                caps["vision_error"] = str(e)[:200]
    old = models_cfg.get_model(mid, with_key=False) or {}
    merged = dict(old.get("capabilities") or {})
    if what != "image":
        for k in ("error", "json_error", "vision_error"):
            merged.pop(k, None)
    else:
        merged.pop("image_error", None)
    merged.update(caps)
    db.update("models", {"id": mid}, {"capabilities": merged})
    return merged


def test_endpoint(eid: str) -> dict:
    """后台“测试”图库接口。"""
    ep = models_cfg.get_endpoint(eid)
    if not ep:
        raise UserError("接口不存在", 404)
    caps: dict = {"tested_at": now()}
    try:
        caps["ok"] = bool(LLM({"id": None}, override=ep).stock_search("office", n=1))
        if not caps["ok"]:
            caps["error"] = "检索没有返回结果（请检查 API Key）"
    except Exception as e:
        caps["ok"] = False
        caps["error"] = str(e)[:300]
    db.update("endpoints", {"id": eid}, {"capabilities": caps})
    return caps
