"""浏览器端到端测试（Playwright）。需要先启动 web、worker 和模拟模型接口（见 tests/README）。

用法：python3 tests/e2e_ui.py http://127.0.0.1:8765 http://127.0.0.1:18999/v1 输出截图目录
"""
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
MOCK = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:18999/v1"
OUT = Path(sys.argv[3] if len(sys.argv) > 3 else "/tmp/e2e_shots")
OUT.mkdir(parents=True, exist_ok=True)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
errors = []


def shot(page, name):
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=False)


def wait_toast(page, text, timeout=120000):
    page.locator(".toast", has_text=text).first.wait_for(timeout=timeout)


def make_files(tmp: Path):
    from app.render.docx_render import render_document
    from app.render.pptx_render import render_deck
    from app.spec.deck import Deck
    from app.spec.document import Document
    from samples import sample_deck, sample_document
    tmp.mkdir(parents=True, exist_ok=True)
    render_deck(Deck.model_validate(sample_deck()), tmp / "已有演示.pptx", {}, tmp / "r")
    render_document(Document.model_validate(sample_document()), tmp / "实验报告.docx", {}, tmp / "r")
    (tmp / "sales.csv").write_text("地区,销售额\n华东,100\n华南,80\n华东,50\n华北,30\n", encoding="utf-8")
    import subprocess
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(tmp), str(tmp / "实验报告.docx")], capture_output=True)
    return tmp


def main():
    files = make_files(OUT / "files")
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console {m.type}: {m.text}") if m.type == "error" else None)

        # 1. 登录
        page.goto(BASE)
        page.get_by_text("管理员登录").click()
        page.get_by_placeholder("用户名").fill("admin")
        page.get_by_placeholder("密码", exact=True).fill("password1234")
        page.get_by_role("button", name="登录").click()
        expect(page.locator(".side")).to_be_visible()
        shot(page, "01_home")

        # 2. 模型接口
        page.goto(BASE + "/#/admin/models")
        # 添加接口后自动拉取模型列表，在列表里勾选
        page.get_by_role("button", name="添加模型接口").click()
        m = page.locator(".modal")
        m.locator("input[type=text]").nth(0).fill("模拟接口")
        m.locator("input[type=text]").nth(1).fill(MOCK)
        m.locator("input[type=password]").fill("test-key")
        m.get_by_role("button", name="保存").click()
        pick = page.locator(".modal", has_text="添加模型：")
        pick.wait_for(timeout=60000)
        pick.get_by_label("mock", exact=True).check()
        pick.get_by_label("mock-img", exact=True).check()
        pick.get_by_role("button", name="添加").click()
        page.wait_for_timeout(600)
        page.get_by_role("button", name="添加图库").click()
        m = page.locator(".modal")
        m.locator("input[type=text]").nth(0).fill("模拟图库")
        m.locator("input[type=text]").nth(1).fill(MOCK.replace("/v1", ""))
        m.locator("input[type=password]").fill("test-key")
        m.get_by_role("button", name="保存").click()
        page.wait_for_timeout(600)
        # 同一个模型负责文字和视觉，另一个负责生图
        page.get_by_role("row", name=re.compile("^mock ")).get_by_role("button", name="测试对话").click()
        page.locator(".modal .badge.done").wait_for(timeout=60000)
        page.wait_for_timeout(1200)
        expect(page.get_by_text("JSON 正常")).to_be_visible()
        for label, model in (("规划", "mock"), ("写作", "mock"), ("视觉", "mock"), ("图像生成", "mock-img")):
            page.get_by_label(re.compile("^" + label)).select_option(label=model)
        page.get_by_label(re.compile("^图库")).select_option(label="模拟图库")
        page.get_by_role("button", name="保存角色").click()
        page.wait_for_timeout(600)
        shot(page, "02_models")

        # 3. 生成 PPT（先确认大纲）
        page.goto(BASE + "/#/create/ppt")
        page.locator("textarea").first.fill("测试主题：年度回顾")
        page.locator("input[type=number]").first.fill("8")
        page.get_by_role("button", name="开始生成").click()
        page.locator(".modal", has_text="确认大纲").wait_for(timeout=120000)
        shot(page, "03_outline")
        page.locator(".modal .outline-edit input[type=text]").nth(3).fill("用户在网页上改的标题")
        page.get_by_role("button", name="确认并继续生成").click()
        page.wait_for_url(re.compile(r"#/work/"), timeout=180000)
        page.locator(".thumbs .th").first.wait_for(timeout=30000)
        expect(page.locator(".thumbs .th")).to_have_count(8)
        page.wait_for_timeout(800)
        shot(page, "04_editor_ppt")

        # 4. 点选元素 + 直接编辑
        page.locator(".thumbs .th").nth(3).click()
        ov = page.locator(".stage .ovl").first
        ov.click()
        expect(page.locator(".scope .chip")).to_have_text(re.compile("选中的 1 个元素"))
        ov.dblclick()
        page.locator(".modal textarea").fill("直接编辑后的标题")
        page.locator(".modal").get_by_role("button", name="保存").click()
        wait_toast(page, "已更新")
        expect(page.locator(".ed-top")).to_contain_text("第 2 版")
        shot(page, "05_direct_edit")

        # 5. 对话修改（选中本页）
        page.get_by_role("button", name="选中本页").click()
        expect(page.locator(".scope .chip")).to_have_text(re.compile("选中的 1 页"))
        page.locator(".chat-input textarea").fill("把标题改得更有力")
        page.locator(".chat-input").get_by_role("button", name="发送").click()
        wait_toast(page, "已更新")
        expect(page.locator(".ed-top")).to_contain_text("第 3 版")
        expect(page.locator(".thumbs .th.changed")).to_have_count(1)
        shot(page, "06_chat_edit")

        # 6. 版本历史与对比
        page.get_by_role("button", name=re.compile("第 3 版")).click()
        page.locator(".modal").get_by_role("button", name="与当前对比").first.click()
        page.locator(".modal .diff-row").first.wait_for()
        shot(page, "07_diff")
        page.keyboard.press("Escape")

        # 7. 下载
        with page.expect_download() as d:
            page.get_by_role("button", name="下载 PPTX").click()
        assert d.value.suggested_filename.endswith(".pptx"), d.value.suggested_filename

        # 8. 签发令牌，访客进入
        page.goto(BASE + "/#/admin/tokens")
        page.get_by_placeholder("例如：张三-毕业答辩").fill("E2E 访客")
        page.get_by_role("button", name="签发", exact=True).click()
        link = page.locator(".modal input[type=text]").first.input_value()
        assert "#redeem=" in link
        page.keyboard.press("Escape")
        shot(page, "08_tokens")
        gctx = b.new_context(viewport={"width": 1280, "height": 800})
        g = gctx.new_page()
        g.on("pageerror", lambda e: errors.append(f"guest pageerror: {e}"))
        g.goto(link)
        expect(g.locator(".side")).to_be_visible()
        expect(g.locator(".side nav")).not_to_contain_text("临时令牌")
        g.goto(BASE + "/#/works")
        g.locator(".empty").wait_for()
        shot(g, "09_guest")

        # 9. 格式转换
        page.goto(BASE + "/#/tools/convert")
        page.locator("input[type=file]").set_input_files(str(files / "实验报告.docx"))
        page.get_by_role("button", name="转换").first.wait_for(timeout=30000)
        page.get_by_role("button", name="转换").first.click()
        page.get_by_role("button", name="下载").first.wait_for(timeout=120000)
        shot(page, "10_convert")

        # 10. PDF 合并
        page.goto(BASE + "/#/tools/pdf")
        page.locator("input[type=file]").set_input_files([str(files / "实验报告.pdf"), str(files / "实验报告.pdf")])
        page.wait_for_timeout(2500)
        page.get_by_role("button", name="开始处理").click()
        page.get_by_role("button", name="下载").first.wait_for(timeout=120000)
        shot(page, "11_pdf_merge")

        # 11. 导入已有 PPT 并原位修改
        page.goto(BASE + "/#/tools/import")
        page.locator("input[type=file]").set_input_files(str(files / "已有演示.pptx"))
        page.wait_for_timeout(2000)
        page.get_by_role("button", name="开始").click()
        page.get_by_role("link", name="打开作品").wait_for(timeout=120000)
        page.get_by_role("link", name="打开作品").click()
        page.locator(".thumbs .th").first.wait_for()
        page.locator(".thumbs .th").nth(1).click()
        page.locator(".chat-input textarea").fill("改一处文字")
        page.locator(".chat-input").get_by_role("button", name="发送").click()
        wait_toast(page, "已更新")
        shot(page, "12_import_edit")

        # 12. Word 与 Excel
        page.goto(BASE + "/#/create/doc")
        page.locator("textarea").first.fill("写一份报告")
        page.locator("input[type=checkbox]").last.uncheck()
        page.get_by_role("button", name="开始生成").click()
        page.wait_for_url(re.compile(r"#/work/"), timeout=180000)
        page.locator(".outline .it").first.wait_for()
        page.locator(".docpage img").first.wait_for()
        page.wait_for_timeout(800)
        shot(page, "13_editor_doc")
        page.goto(BASE + "/#/create/xls")
        page.locator("textarea").first.fill("按地区汇总")
        page.locator("input[type=file]").first.set_input_files(str(files / "sales.csv"))
        page.wait_for_timeout(1500)
        page.get_by_role("button", name="开始生成").click()
        page.wait_for_url(re.compile(r"#/work/"), timeout=180000)
        page.locator("table.sheet").wait_for()
        page.get_by_role("button", name="按地区汇总").click()
        expect(page.locator("table.sheet")).to_contain_text("260")
        shot(page, "14_editor_xls")

        # 13. 作品库、任务中心、后台页面
        for path, name in (("/#/works", "15_works"), ("/#/jobs", "16_jobs"), ("/#/admin/usage", "17_usage"), ("/#/admin/system", "18_system"),
                           ("/#/admin/settings", "19_settings"), ("/#/admin/workspaces", "20_workspaces"), ("/#/admin/audit", "21_audit"), ("/#/files", "22_files"), ("/#/account", "23_account")):
            page.goto(BASE + path)
            page.wait_for_timeout(1200)
            shot(page, name)
        b.close()
    bad = [e for e in errors if "401" not in e and "Failed to load resource" not in e]
    print("前端错误：", bad if bad else "无")
    print("E2E 完成，截图目录：", OUT)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
