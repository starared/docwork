# DocWork 文档工作台

DocWork 是一个自托管的 AI 文档工作台。接入你自己的大模型接口后，可以用 AI 生成和修改 PPT、Word、Excel，也可以处理已有的文件，并提供格式转换、PDF 工具和文字识别。生成的都是原生 Office 文件，标题样式、图表、公式、表格都是真实对象，可以直接在 Microsoft Office 或 WPS 中继续编辑。

## 部署

需要一台装有 Docker 和 Docker Compose 的 Linux 服务器。

```bash
git clone https://github.com/starared/docwork.git
cd docwork
cp .env.example .env
```

生成一个主密钥，填到 `.env` 的 `DW_MASTER_KEY`（用于加密保存模型接口的密钥，设置后不要再改）：

```bash
openssl rand -hex 32
```

创建数据目录，构建并启动：

```bash
mkdir -p data && sudo chown 1000:1000 data
docker compose build
docker compose run --rm -it web python -m app.cli create-owner    # 创建管理员账号
docker compose up -d
```

浏览器打开 `http://服务器IP:8000`，用管理员账号登录。然后在后台“模型接口”中添加一个 OpenAI 兼容的模型接口（地址和 API Key），拉取模型后在“角色分配”中为各项工作选择模型，就可以开始使用了。

更新到新版本：

```bash
git pull && docker compose build && docker compose up -d
```

## 功能

### AI 生成 PPT

输入主题，可以上传资料，AI 先规划大纲，你可以修改大纲后再生成。支持 21 种版式和多套主题配色，包含原生图表（数据可编辑）、表格、图标、配图和演讲备注。生成后会自动检查文字溢出、遮挡等排版问题并修正。

### AI 生成 Word

支持报告、项目方案、论文、会议纪要、说明书、公文六种文档类型。包含真实的标题样式和目录、公式、原生图表、表格、题注和参考文献。

### AI 生成 Excel

上传 CSV 或 Excel 数据后，由程序计算筛选、分组、透视等分析结果，合计行写成真实公式并画图；没有数据时由 AI 设计表格，例如预算表、记录表。

### 修改与版本

- 用对话修改整份文件，或者只修改选中的页面、元素、段落、单元格区域
- 直接编辑文字和数据
- 每次修改都会生成新版本，可以对比任意两个版本、恢复旧版本、给版本加星标

### 处理已有文件

- 导入 PPTX、DOCX、XLSX 后用对话修改，保留原有的母版、版式和格式；Word 可以用修订模式显示改动
- 旧格式（doc、ppt、xls）自动转换；带宏的文件导入时会移除宏
- PDF 和图片可以重建为可编辑的 PPT 或 Word

### 联网资料

生成 PPT 和 Word 时，可以让 AI 按主题搜索网页作为资料（需要一个 SearXNG 搜索服务），也可以直接给出参考网页。引用的内容会标注来源编号，Word 末尾自动附参考文献。

### 格式转换

Office 转 PDF，PDF 转 Word、Excel、PPT、图片、文字，图片转 PDF，Markdown 和 HTML 转 Word 或 PDF，Word 转 Markdown，CSV 与 Excel 互转。每种转换都会标明还原程度。

### PDF 工具与文字识别

- PDF 合并、拆分、压缩、旋转、调整页序和删页，提取文字和图片
- 识别扫描件和图片中的文字（中文优先），可以输出带文字层的 PDF、纯文本或 Word

### 作品库

所有生成和导入的文件集中管理，支持文件夹、标签、星标、全文搜索、回收站和批量打包下载。

### 访问控制与管理

- 一个管理员账号，可以开启两步验证
- 给其他人发放临时访问令牌，可以设置有效期、一次性使用、可用的功能和用量上限；每个令牌有独立的工作区，撤销后立即失效
- 后台可以查看任务、用量统计、系统状态和操作日志

## 更新记录

见 [CHANGELOG.md](CHANGELOG.md)。
