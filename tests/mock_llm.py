"""模拟的 OpenAI 兼容接口，用于端到端测试。根据系统提示词判断任务类型，返回合法（或故意不合法）的 JSON。"""
from __future__ import annotations

import base64
import io
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALLS: list[dict] = []
STATE = {"bad_cards_once": True, "no_json_mode": False}


def _outline(user: str) -> dict:
    m = re.search(r"页数：(\d+)", user)
    n = int(m.group(1)) if m else 8
    intents = ["cover", "toc", "section", "points", "compare", "data", "process", "metrics", "table", "timeline", "quote", "points"]
    slides = []
    for i in range(n):
        if i == 0:
            it = "cover"
        elif i == n - 1:
            it = "ending"
        else:
            it = intents[1 + (i - 1) % (len(intents) - 1)]
        slides.append({"title": f"第{i + 1}页：{it}要点", "intent": it, "points": [f"要点 {k + 1}，数据 {10 * (k + 1)}%" for k in range(3)], "data": "Q1 10 Q2 20"})
    return {"title": "模拟演示文稿", "subtitle": "测试", "slides": slides}


def _slide(item: dict, bad: bool = False) -> dict:
    it = item.get("intent")
    t = item.get("title", "标题")[:30]
    pts = item.get("points") or ["要点"]
    if it == "cover":
        return {"layout": "cover", "title": t, "content": {"subtitle": "副标题", "author": "测试", "date": "2026"}, "notes": "开场"}
    if it == "ending":
        return {"layout": "ending", "title": "谢谢", "content": {"subtitle": "欢迎交流"}}
    if it == "toc":
        return {"layout": "toc", "title": "目录", "content": {"items": ["第一章", "第二章", "第三章"]}}
    if it == "section":
        return {"layout": "section", "title": t, "content": {"number": "01", "subtitle": "章节"}}
    if it == "compare":
        head = "这是一个明显超过十六个字上限的卡片标题文字" if bad else "卡片标题"
        return {"layout": "cards", "title": t, "content": {"cards": [{"heading": head, "body": "说明文字", "icon": "star"}, {"heading": "第二张", "body": "说明", "icon": "gear"}, {"heading": "第三张", "body": "说明", "icon": "idea"}]}}
    if it == "data":
        return {"layout": "chart", "title": t, "content": {"chart": {"type": "column", "title": "季度", "categories": ["Q1", "Q2", "Q3"], "series": [{"name": "收入", "values": [10, 20, 30]}]}, "bullets": ["增长明显"]}}
    if it == "process":
        return {"layout": "process", "title": t, "content": {"steps": [{"label": f"步骤{k}", "detail": "说明"} for k in range(1, 5)]}}
    if it == "metrics":
        return {"layout": "big_number", "title": t, "content": {"metrics": [{"value": "38%", "label": "增长率", "detail": "同比"}, {"value": "12亿", "label": "收入"}]}}
    if it == "table":
        return {"layout": "table", "title": t, "content": {"table": {"columns": ["项目", "数值"], "rows": [["甲", 1], ["乙", 2]]}}}
    if it == "timeline":
        return {"layout": "timeline", "title": t, "content": {"events": [{"date": f"2026.0{k}", "label": f"事件{k}"} for k in range(1, 5)]}}
    if it == "quote":
        return {"layout": "image_text", "title": t, "content": {"image": {"query": "office team", "prompt": "office"}, "bullets": [{"text": p} for p in pts]}}
    return {"layout": "bullets", "title": t, "content": {"bullets": [{"text": p} for p in pts]}, "notes": "讲解"}


def respond(system: str, user) -> str:
    u = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False)
    if "只回复两个字" in u:
        return "你好"
    if '输出 {"a": 1' in u:
        return '{"a": 1, "b": "测试"}'
    if "主要是什么颜色" in u:
        return "红色"
    if "幻灯片的渲染图" in u:
        return '{"ok": true, "problems": []}'
    if "需要在搜索引擎中查找资料" in system:
        return '{"queries": ["模拟检索一", "模拟检索二"]}'
    if "规划一份演示文稿的大纲" in system:
        return json.dumps(_outline(u), ensure_ascii=False)
    if "为演示文稿选择主题" in system:
        return '{"preset": "清新青", "decor": "underline", "cover": "split", "reason": "测试"}'
    if "页面不合格" in u and "为指定页面编写页面规格" in system:
        items = re.findall(r"- 大纲：(\{.*?\})\n", u)
        return json.dumps({"slides": [_slide(json.loads(x)) for x in items]}, ensure_ascii=False)
    if "为指定页面编写页面规格" in system and "拆成两页" in system:
        s = json.loads(u)
        return json.dumps({"slides": [s, dict(s, title=s["title"][:30] + "（续）")]}, ensure_ascii=False)
    if "为指定页面编写页面规格" in system:
        m = re.search(r"本次需要编写第 \d+–\d+ 页：\n(\[.*?\])\n\n主题", u, re.S)
        items = json.loads(m.group(1)) if m else []
        bad = STATE["bad_cards_once"]
        out = [_slide(x, bad=bad) for x in items]
        if bad and any(x.get("intent") == "compare" for x in items):
            STATE["bad_cards_once"] = False
        return json.dumps({"slides": out}, ensure_ascii=False)
    if "精简到目标字数" in system:
        items = json.loads(u.split("\n", 1)[1])
        return json.dumps({"items": [{"id": x["id"], "text": x["text"]} for x in items]}, ensure_ascii=False)
    if "压缩为要点摘要" in system:
        return json.dumps({"summary": "摘要：" + u[:200]}, ensure_ascii=False)
    if "规划一份 Word 文档的结构" in system:
        return json.dumps({"title": "模拟报告", "preset": "report", "meta": {"author": "测试"}, "toc": True,
                           "sections": [{"heading": "背景", "level": 1, "points": ["a"]}, {"heading": "方法", "level": 1, "points": ["b"], "elements": ["equation", "table"]},
                                        {"heading": "细节", "level": 2, "points": ["c"]}, {"heading": "结论", "level": 1, "points": ["d"]}]}, ensure_ascii=False)
    if "为文档写出指定章节的完整内容" in system:
        sec = json.loads(re.search(r"现在撰写：(\{.*?\})\n", u).group(1))
        blocks = [{"type": "heading", "level": sec.get("level", 1), "text": sec["heading"]},
                  {"type": "paragraph", "text": f"这是关于{sec['heading']}的正文，含有**重点**和数据 42%。"}]
        if "equation" in (sec.get("elements") or []):
            blocks += [{"type": "equation", "latex": "E = mc^2", "number": True},
                       {"type": "table", "table": {"caption": "结果", "columns": ["x", "y"], "rows": [[1, 2], [3, 4]]}}]
        m = re.search(r"来源：(http\S+/page/a)", u)
        if m and sec["heading"] == "结论":
            # 模拟模型不听话，把网络资料又写进参考文献
            blocks.append({"type": "references", "items": [f"某作者. 网页甲[EB/OL]. {m.group(1)}.", "张三. 某本书[M]. 北京: 出版社, 2020."]})
        return json.dumps({"blocks": blocks}, ensure_ascii=False)
    if "制定分析方案" in system:
        name = re.search(r"数据表 (\S+)：", u).group(1)
        return json.dumps({"title": "销售分析", "summary": "华东最高。", "sheets": [
            {"name": "按地区汇总", "source": name, "ops": [{"op": "group", "by": ["地区"], "aggs": [{"column": "销售额", "func": "sum", "as": "销售额合计"}]},
                                                          {"op": "sort", "by": "销售额合计", "ascending": False}],
             "totals": True, "chart": {"type": "column", "title": "各地区销售额", "x": "地区", "y": ["销售额合计"]}, "note": "各地区销售额"}]}, ensure_ascii=False)
    if "你是 Excel 专家" in system:
        return json.dumps({"title": "预算表", "sheets": [{"name": "预算", "columns": [{"header": "项目"}, {"header": "金额", "number_format": "#,##0"}],
                                                          "rows": [["房租", 3000], ["餐饮", 1500], ["合计", "=SUM(B2:B3)"]]}]}, ensure_ascii=False)
    if "演示文稿编辑" in system:
        sid = re.search(r'"id": ?"(s[0-9a-f]+)"', u)
        sid = sid.group(1) if sid else re.search(r"id=(s[0-9a-f]+)", u).group(1)
        return json.dumps({"ops": [{"op": "set_field", "slide_id": sid, "path": "title", "value": "修改后的标题"}], "reply": "已修改标题"}, ensure_ascii=False)
    if "你是文档编辑。根据用户的修改指令" in system:
        bid = re.search(r'"id": ?"(b[0-9a-f]+)", ?"type": ?"paragraph"', u).group(1)
        return json.dumps({"ops": [{"op": "set_text", "block_id": bid, "text": "修改后的段落。"}], "reply": "已修改"}, ensure_ascii=False)
    if "你是 Excel 编辑" in system:
        return json.dumps({"ops": [{"op": "set_cell", "sheet": "预算", "cell": "B2", "value": 3500}], "reply": "房租改为 3500"}, ensure_ascii=False)
    if "可以修改的内容单元" in system:
        units = json.loads(re.search(r"可修改的内容单元：\n(\[.*\])\n\n用户指令", u, re.S).group(1))
        first = next(x for x in units if x.get("text"))
        return json.dumps({"ops": [{"op": "set_text", "unit": first["id"], "text": "原位修改后的文字"}], "reply": "已改一处"}, ensure_ascii=False)
    return "{}"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.headers.get("Authorization") != "Bearer test-key":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"bad key"}')
            return
        if body.get("model") == "mock-chatimg":
            # 只能通过对话接口出图的模型（类似经 new-api 转发的 Gemini 图像模型）
            if self.path.endswith("/images/generations"):
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"error":{"code":"model_not_found","message":"No available channel"}}')
                return
            from PIL import Image
            im = Image.new("RGB", (640, 360), (30, 160, 90))
            buf = io.BytesIO()
            im.save(buf, "JPEG")
            url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
            self._json({"choices": [{"message": {"role": "assistant", "content": None,
                                                 "images": [{"type": "image_url", "image_url": {"url": url}}]}}],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 1290}})
            return
        if self.path.endswith("/images/generations"):
            from PIL import Image
            im = Image.new("RGB", (800, 450), (90, 140, 200))
            buf = io.BytesIO()
            im.save(buf, "PNG")
            self._json({"data": [{"b64_json": base64.b64encode(buf.getvalue()).decode()}]})
            return
        if STATE["no_json_mode"] and "response_format" in body:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"response_format not supported"}')
            return
        msgs = body.get("messages", [])
        system = next((m["content"] for m in msgs if m["role"] == "system"), "")
        users = [m["content"] for m in msgs if m["role"] == "user"]
        user = users[-1] if users else ""
        if len(users) > 1 and "不合格" in str(user):
            user = users[0]
        text = respond(system, users[0] if len(users) > 1 else user)
        CALLS.append({"system": system[:60], "stream": bool(body.get("stream"))})
        usage = {"prompt_tokens": 100, "completion_tokens": len(text) // 2}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(0, len(text), 40):
                chunk = {"choices": [{"delta": {"content": text[i:i + 40]}}]}
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._json({"choices": [{"message": {"content": text}}], "usage": usage})

    def do_GET(self):
        if self.path.endswith("/v1/models"):
            self._json({"object": "list", "data": [{"id": "mock", "object": "model"}, {"id": "mock-img", "object": "model"}]})
            return
        if "/v1/search" in self.path:
            self._json({"photos": [{"src": {"large2x": f"http://127.0.0.1:{self.server.server_port}/img.png"}, "photographer": "测试"}]})
            return
        if self.path.startswith("/search?"):
            # 模拟 SearXNG 的 JSON 接口：第二个结果是读不到的网页，应退回摘要
            base = f"http://127.0.0.1:{self.server.server_port}"
            self._json({"query": "q", "results": [
                {"url": base + "/page/a", "title": "网页甲", "content": "甲的摘要"},
                {"url": base + "/page/missing", "title": "网页乙", "content": "乙的摘要：增长 12%"},
                {"url": "ftp://example.com/x", "title": "不是网页", "content": ""}]})
            return
        if self.path.startswith("/page/a") or self.path == "/redirect":
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/page/a")
                self.end_headers()
                return
            data = ("<html><head><title>网页甲标题</title><script>var x=1;</script></head><body><nav>导航</nav>"
                    "<article><h1>正文标题</h1><p>2025 年市场规模达到 3.2 万亿元，同比增长 8.5%。</p>"
                    "<p>第二段：行业集中度继续提高。</p></article><footer>版权所有</footer></body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.endswith("/img.png"):
            from PIL import Image
            im = Image.new("RGB", (1200, 800), (200, 120, 60))
            buf = io.BytesIO()
            im.save(buf, "PNG")
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()

    def _json(self, d):
        data = json.dumps(d, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_port
