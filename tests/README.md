# 测试

## 单元与集成测试

在项目根目录执行（需要先按 README 安装 Python 依赖和系统组件；`test_pipeline` 会真实调用 LibreOffice）：

```bash
cd tests
DW_SANDBOX=rlimit python -m unittest test_core test_api test_regressions test_tools test_research test_pipeline
```

- `test_core`：密码、TOTP、令牌、会话、任务队列、配额、请求来源地址、沙箱。
- `test_api`：Web 接口的登录、CSRF、隔离与越权、上传下载、配额、后台。
- `test_regressions`：历史问题的回归用例。
- `test_tools`：渲染器、格式转换、PDF 工具、宏剥离、沙箱超时。
- `test_research`：联网资料（网页正文、内网拦截、SearXNG 流程）。
- `test_pipeline`：模拟模型接口 + 真实 worker 的端到端生成、修改、导入、工具任务。

没有安装 OCR 引擎（RapidOCR 或 Tesseract）时，`test_pipeline.test_06_tools` 的 OCR 步骤会失败；
没有中文字体时，预览图的像素差异可能让 `test_05` 的页面变化计数超出容差。

## 浏览器端到端测试

`e2e_ui.py` 用 Playwright 驱动真实浏览器走一遍主要流程，需要先启动三样东西：

1. 模拟模型接口：`python -c "import mock_llm; s, p = mock_llm.start(); print(p); import time; time.sleep(1e9)"`（在 `tests` 目录下）。
2. Web 与 worker：按 README 的直接部署方式启动，`DW_COOKIE_SECURE=false`，并已创建管理员 `admin / password1234`。
3. 然后运行：`python tests/e2e_ui.py http://127.0.0.1:8000 http://127.0.0.1:<模拟接口端口>/v1 /tmp/e2e_shots`。

脚本会在后台"模型接口"页面添加模拟接口并分配角色，截图保存在最后一个参数指定的目录。
