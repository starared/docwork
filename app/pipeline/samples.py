"""测试与兼容性测试包共用的样例规格：覆盖全部布局、图表类型和文档预设。"""


def sample_deck(image_asset: str | None = None) -> dict:
    img = {"query": "城市夜景", "asset": image_asset}
    return {
        "title": "2026 年度业务回顾",
        "theme": {"name": "商务蓝", "primary": "#1F4E79", "secondary": "#2E75B6", "accent": "#F4B183", "decor": "bar", "cover": "split"},
        "footer": "DocWork 示例",
        "slides": [
            {"layout": "cover", "title": "2026 年度业务回顾与 2027 规划", "content": {"subtitle": "增长、效率与新方向", "author": "战略部", "date": "2026 年 9 月"}, "notes": "开场白：感谢大家。"},
            {"layout": "toc", "title": "目录", "content": {"items": ["年度概览", "核心指标", "业务进展", "挑战与对策", "2027 规划", "附录"]}},
            {"layout": "section", "title": "年度概览", "content": {"number": "01", "subtitle": "全年关键事件与成绩"}},
            {"layout": "bullets", "title": "全年总体表现超出预期", "content": {"bullets": [
                {"text": "营业收入 12.8 亿元，同比增长 **23%**", "sub": ["海外业务贡献 31%", "新产品线占比首次超过 20%"]},
                {"text": "毛利率提升 2.4 个百分点，达到 41.6%"},
                {"text": "客户数量突破 5,000 家，续约率 92%"},
                {"text": "研发投入占收入 15%，新增专利 48 项"}]}},
            {"layout": "two_column", "title": "优势与不足", "content": {"left": {"heading": "做得好的", "bullets": ["产品迭代速度快", "大客户服务口碑好", "成本控制有效"]},
                                                                        "right": {"heading": "需要改进", "bullets": ["中小客户获客成本偏高", "海外交付周期较长", "数据平台建设滞后"]}}},
            {"layout": "cards", "title": "三大战略方向", "content": {"cards": [
                {"heading": "产品智能化", "body": "在核心产品中全面引入 AI 能力，提升自动化水平。", "icon": "idea"},
                {"heading": "市场国际化", "body": "重点拓展东南亚与中东市场，建立本地团队。", "icon": "globe"},
                {"heading": "运营数字化", "body": "建设统一数据平台，打通销售、交付与财务。", "icon": "chart"}]}},
            {"layout": "quote", "title": "客户评价", "content": {"quote": "他们不只是供应商，更像是一起解决问题的合作伙伴。", "source": "某制造业集团 CIO"}},
            {"layout": "image_text", "title": "新总部园区投入使用", "content": {"image": img, "bullets": [{"text": "建筑面积 3.2 万平方米"}, {"text": "可容纳 1,500 名员工"}, {"text": "获得 LEED 金级认证"}], "image_side": "right", "caption": "新园区外景"}},
            {"layout": "full_image", "title": "走向更广阔的市场", "content": {"image": img, "caption": "2027 年将在 5 个国家设立办事处"}},
            {"layout": "image_grid", "title": "年度活动掠影", "content": {"images": [{"image": img, "caption": "客户大会"}, {"image": img, "caption": "技术峰会"}, {"image": img, "caption": "公益活动"}]}},
            {"layout": "process", "title": "新客户交付流程", "content": {"steps": [
                {"label": "需求调研", "detail": "两周内完成业务访谈"}, {"label": "方案设计", "detail": "输出实施方案与排期"},
                {"label": "系统部署", "detail": "标准化部署，平均 5 天"}, {"label": "培训上线", "detail": "分角色培训与试运行"},
                {"label": "持续运营", "detail": "月度回访与优化"}]}},
            {"layout": "timeline", "title": "2026 年关键里程碑", "content": {"events": [
                {"date": "2026.03", "label": "完成 B 轮融资", "detail": "融资 5 亿元"}, {"date": "2026.05", "label": "新产品发布", "detail": "智能分析平台上线"},
                {"date": "2026.08", "label": "海外首站", "detail": "新加坡办公室开业"}, {"date": "2026.11", "label": "客户破五千", "detail": "服务客户超过 5,000 家"}]}},
            {"layout": "matrix", "title": "产品组合分析", "content": {"x_label": "市场增长率", "y_label": "相对市场份额", "quadrants": [
                {"heading": "明星产品", "body": "智能分析平台：高增长、高份额"}, {"heading": "问题产品", "body": "移动端工具：增长快但份额低"},
                {"heading": "现金牛", "body": "报表系统：稳定贡献利润"}, {"heading": "瘦狗产品", "body": "旧版客户端：计划逐步下线"}]}},
            {"layout": "hierarchy", "title": "组织架构调整", "content": {"root": "总经理", "children": [
                {"label": "产品中心", "items": ["产品规划", "用户研究"]}, {"label": "研发中心", "items": ["平台研发", "AI 实验室"]},
                {"label": "市场中心", "items": ["品牌", "渠道"]}, {"label": "运营中心", "items": ["交付", "客户成功"]}]}},
            {"layout": "comparison", "title": "自建与采购方案对比", "content": {"left": {"heading": "自建", "bullets": ["初期投入高", "可深度定制", "周期约 9 个月"]},
                                                                               "right": {"heading": "采购", "bullets": ["初期投入低", "功能标准化", "周期约 2 个月"]}, "verdict": "建议核心模块自建，通用模块采购"}},
            {"layout": "chart", "title": "季度收入持续增长", "content": {"chart": {"type": "column", "title": "季度收入（亿元）", "categories": ["Q1", "Q2", "Q3", "Q4"],
                "series": [{"name": "2025", "values": [2.1, 2.4, 2.6, 3.3]}, {"name": "2026", "values": [2.6, 3.0, 3.2, 4.0]}], "source": "公司财务报表"},
                "bullets": ["四个季度均实现同比增长", "Q4 增速最快，达 21%"]}},
            {"layout": "chart", "title": "收入构成", "content": {"chart": {"type": "doughnut", "categories": ["国内企业", "海外", "政府", "其他"], "series": [{"name": "占比", "values": [52, 31, 12, 5]}]}}},
            {"layout": "chart", "title": "利润变动分析", "content": {"chart": {"type": "waterfall", "categories": ["2025 利润", "收入增长", "成本上升", "费用节约"], "series": [{"name": "变动", "values": [3.2, 1.8, -0.9, 0.4]}]}}},
            {"layout": "big_number", "title": "核心指标", "content": {"metrics": [{"value": "12.8亿", "label": "营业收入", "detail": "同比增长 23%"}, {"value": "41.6%", "label": "毛利率", "detail": "提升 2.4 个百分点"}, {"value": "92%", "label": "客户续约率", "detail": "行业平均约 80%"}]}},
            {"layout": "table", "title": "区域业绩明细", "content": {"table": {"columns": ["区域", "收入（亿元）", "同比", "客户数"], "rows": [
                ["华东", 4.2, "21%", 1820], ["华南", 3.1, "25%", 1250], ["华北", 2.3, "18%", 960], ["海外", 3.2, "46%", 1010]], "caption": "单位：亿元；数据截至 2026 年 12 月"}}},
            {"layout": "chart_table", "title": "产品线表现", "content": {"chart": {"type": "bar", "categories": ["分析平台", "报表系统", "移动工具"], "series": [{"name": "收入", "values": [5.1, 4.9, 2.8]}]},
                "table": {"columns": ["产品线", "增速"], "rows": [["分析平台", "48%"], ["报表系统", "6%"], ["移动工具", "35%"]]}}},
            {"layout": "team", "title": "核心管理团队", "content": {"people": [{"name": "张明", "role": "首席执行官", "bio": "20 年企业软件经验"}, {"name": "李华", "role": "首席技术官", "bio": "负责平台与 AI 研发"}, {"name": "王芳", "role": "首席运营官", "bio": "主导交付与客户成功"}]}},
            {"layout": "ending", "title": "谢谢", "content": {"subtitle": "欢迎交流与指导", "contact": "strategy@example.com"}},
        ],
    }


def sample_document() -> dict:
    return {
        "title": "新型荧光探针检测尿酸的实验报告",
        "preset": "report",
        "meta": {"author": "课题组", "date": "2026 年 9 月", "organization": "某大学化学学院"},
        "header_text": "实验报告",
        "blocks": [
            {"type": "toc"},
            {"type": "heading", "level": 1, "text": "研究背景"},
            {"type": "paragraph", "text": "尿酸是嘌呤代谢的终产物，血清尿酸浓度异常与**痛风**、肾病等多种疾病相关。本实验采用分子印迹聚合物（MIP）修饰的荧光探针，实现对尿酸的高选择性检测。"},
            {"type": "heading", "level": 2, "text": "实验目的"},
            {"type": "list", "ordered": True, "items": ["合成 MIP 荧光探针", {"text": "评价探针的检测性能", "sub": ["线性范围", "检出限", "选择性"]}, "应用于实际样品检测"]},
            {"type": "heading", "level": 1, "text": "实验方法"},
            {"type": "paragraph", "text": "检出限按下式计算，其中 σ 为空白样品标准偏差，k 为校准曲线斜率："},
            {"type": "equation", "latex": "LOD = \\frac{3\\sigma}{k}", "number": True},
            {"type": "equation", "latex": "F_0/F = 1 + K_{SV}[Q]", "number": True},
            {"type": "equation", "latex": "\\bar{x} = \\frac{1}{n}\\sum_{i=1}^{n} x_i", "number": False},
            {"type": "heading", "level": 1, "text": "结果与讨论"},
            {"type": "table", "table": {"caption": "不同浓度尿酸的荧光猝灭效率", "columns": ["浓度（μM）", "F₀/F", "RSD（%）"], "rows": [[0, 1.00, 1.2], [5, 1.18, 1.8], [10, 1.37, 2.1], [20, 1.74, 1.6], [40, 2.51, 2.4]]}},
            {"type": "chart", "chart": {"type": "line", "title": "Stern-Volmer 曲线", "categories": ["0", "5", "10", "20", "40"], "series": [{"name": "F0/F", "values": [1.0, 1.18, 1.37, 1.74, 2.51]}]}, "caption": "尿酸浓度与荧光猝灭的关系"},
            {"type": "quote", "text": "探针在 0–40 μM 范围内线性良好（R² = 0.998）。", "source": "实验记录"},
            {"type": "heading", "level": 1, "text": "结论"},
            {"type": "paragraph", "text": "所制备的 MIP 荧光探针对尿酸具有良好的选择性和灵敏度，检出限为 0.21 μM，可用于血清样品中尿酸的快速检测。"},
            {"type": "references", "style": "gbt7714", "items": ["张三, 李四. 分子印迹荧光传感器研究进展[J]. 分析化学, 2024, 52(3): 301-310."]},
        ],
    }


def sample_workbook() -> dict:
    return {
        "title": "季度销售统计",
        "sheets": [{
            "name": "销售数据",
            "columns": [{"header": "月份"}, {"header": "华东", "number_format": "#,##0"}, {"header": "华南", "number_format": "#,##0"}, {"header": "合计", "number_format": "#,##0"}, {"header": "增长率", "number_format": "0.0%"}],
            "rows": [
                ["1月", 1200, 980, "=B2+C2", None],
                ["2月", 1350, 1020, "=B3+C3", "=D3/D2-1"],
                ["3月", 1500, 1100, "=B4+C4", "=D4/D3-1"],
                ["合计", "=SUM(B2:B4)", "=SUM(C2:C4)", "=SUM(D2:D4)", None],
            ],
            "cond_formats": [{"range": "E3:E4", "kind": "greater_than", "value": 0.1, "color": "#C6EFCE"}],
            "validations": [],
            "charts": [{"type": "column", "title": "月度销售", "categories": "A2:A4", "values": ["B1:B4", "C1:C4"], "anchor": "G2"}],
            "expected": [{"cell": "D5", "value": 7150}, {"cell": "B5", "value": 4050}],
        }],
    }


def chart_deck(types) -> dict:
    """每种图表类型一页。"""
    slides = [{"layout": "cover", "title": "图表类型测试", "content": {"subtitle": "原生图表可在 Office/WPS 中“编辑数据”"}}]
    cats = ["一月", "二月", "三月", "四月", "五月"]
    for t in types:
        if t in ("scatter", "bubble"):
            ch = {"type": t, "title": t, "point_series": [{"name": "样本", "points": [{"x": i, "y": i * i % 7 + 1, "size": i + 1} for i in range(1, 8)]}]}
        elif t == "sankey":
            ch = {"type": t, "title": t, "flows": [{"source": "收入", "target": "成本", "value": 60}, {"source": "收入", "target": "利润", "value": 40},
                                                    {"source": "成本", "target": "人力", "value": 35}, {"source": "成本", "target": "其他", "value": 25}]}
        elif t in ("pie", "doughnut", "funnel", "waterfall"):
            ch = {"type": t, "title": t, "categories": cats, "series": [{"name": "数值", "values": [40, 25, 15, 12, 8] if t != "waterfall" else [10, 4, -3, 2, -1]}]}
        else:
            ch = {"type": t, "title": t, "categories": cats, "series": [{"name": "甲", "values": [3, 5, 4, 6, 7]}, {"name": "乙", "values": [2, 3, 5, 4, 6]}]}
        slides.append({"layout": "chart", "title": f"图表：{t}", "content": {"chart": ch, "bullets": ["原生图表" if t in ("column", "bar", "line", "area", "pie", "doughnut", "scatter", "radar", "bubble") else "图片图表（附数据 XLSX）"]}})
    slides.append({"layout": "ending", "title": "结束", "content": {}})
    return {"title": "图表类型测试", "slides": slides}


COMPAT_README = """兼容性测试包使用方法

请在约定版本的 Windows 版 Microsoft Office 和 WPS 中逐一打开本包中的文件，检查：
1. 打开时是否提示“需要修复”；
2. 页数、段落、表格是否完整，文字是否溢出或被遮挡；
3. PPT 中的原生图表能否“编辑数据”，文字、形状、表格能否直接编辑；
4. Word 中的目录能否更新、公式能否编辑、表头是否在跨页时重复；
5. Excel 打开后是否直接显示计算结果、图表和条件格式是否正常；
6. 编辑并保存后再次打开，内容是否保持不变。

Word 样例分“系统字体”（微软雅黑、宋体、仿宋_GB2312 等 Windows 自带字体）和“思源字体”两种；
思源字体版本需要电脑上安装思源黑体、思源宋体后才能正确显示。
发现的问题请记录文件名和现象，用于调整生成规则。
"""
