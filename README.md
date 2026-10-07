# DocWork 文档工作台

自托管的 AI 文档工作台：接入你自己的模型接口，生成和修改 PPT、Word、Excel，另外提供格式转换、PDF 工具和 OCR。输出的都是原生 Office 文件（真实的标题样式、图表、公式、表格），可以直接在 Microsoft Office 或 WPS 中继续编辑。

## 功能

- **AI 生成**：PPT（21 种版式、主题配色、原生图表、配图、演讲备注，可先确认大纲）；Word（报告、方案、论文、纪要、说明书、公文，带目录、公式、图表、参考文献）；Excel（上传数据后由程序计算分析结果，合计写成真实公式）
- **修改**：对话修改整份或选中的页面、段落、单元格区域；也可以直接编辑。每次修改生成新版本，可以对比和恢复
- **已有文件**：导入 PPTX / DOCX / XLSX（旧格式自动转换，宏会被移除）后原位修改，保留原有母版和格式；PDF 和图片可以重建为可编辑的 PPT 或 Word
- **联网资料（可选）**：按题目搜索网页作为资料，或直接给出参考网页，引用处标注来源
- **格式转换与 PDF 工具**：Office ↔ PDF、PDF → Word / Excel / 图片 / 文字、Markdown → Word 等；PDF 合并、拆分、压缩、旋转、调整页序、提取文字和图片；OCR（中文优先）
- **访问控制**：一个管理员账号；给其他人发临时访问令牌，可以限制有效期、功能和用量，每个令牌有独立的工作区

## 运行要求

- 已安装 Docker 和 Docker Compose（v2，`docker compose` 命令）的 Linux 服务器，x86_64 和 ARM64 都可以
- 内存：LibreOffice、OCR 等处理程序比较占内存。默认配置按 4 核 24GB 设置，内存较小的机器按下文“资源与并发”调低
- 一个 OpenAI 兼容格式的模型接口（地址 + API Key），见下文“配置模型”

## 快速开始

```bash
git clone https://github.com/starared/docwork.git
cd docwork
cp .env.example .env
```

编辑 `.env`，至少填写：

| 配置项 | 说明 |
|---|---|
| `DW_MASTER_KEY` | 加密保存 API Key 用的主密钥，用 `openssl rand -hex 32` 生成。设置后不要再改，否则已保存的 Key 无法解密 |
| `DW_PUBLIC_URL` | 浏览器访问的地址，例如 `http://服务器IP:8000` |

其余配置都有默认值：数据保存在项目目录下的 `data/`，网页监听 `0.0.0.0:8000`。

容器内的程序以 UID 1000 运行，需要能写数据目录：

```bash
mkdir -p data && sudo chown 1000:1000 data
```

构建并启动（第一次构建要下载 LibreOffice 和字体，需要一些时间）：

```bash
docker compose build
docker compose run --rm web python -m app.cli check              # 环境自检
docker compose run --rm -it web python -m app.cli create-owner    # 创建管理员账号
docker compose up -d
```

打开 `DW_PUBLIC_URL`，用管理员账号登录。服务器有防火墙或云安全组时，需要放行端口。

## 配置模型

后台 →“模型接口”→“添加模型接口”，填写接口地址（到 `/v1` 为止）和 API Key，保存后拉取模型列表并勾选要用的模型。任何 OpenAI 兼容接口都可以，例如：

| 服务 | 接口地址 |
|---|---|
| OpenAI | `https://api.openai.com/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| 本机的 Ollama | `http://host.docker.internal:11434/v1`（Ollama 需监听 `0.0.0.0`，见下文“访问宿主机或其他容器中的服务”） |
| One API / New API 等聚合网关 | 网关地址加 `/v1` |

然后在“角色分配”中给各项工作指定模型。同一个模型可以担任多个角色，没有指定的角色对应功能不可用：

| 角色 | 用途 | 要求 |
|---|---|---|
| 规划、写作、快速 | 大纲、正文、摘要等 | 支持 `/v1/chat/completions`，最好支持 JSON 输出 |
| 视觉 | 检查渲染后的页面有没有排版问题、识别图片内容 | 模型支持图片输入 |
| 图像生成 | 给 PPT 和 Word 生成配图 | 支持 `/v1/images/generations`；只能在对话里返回图片的模型（例如部分 Gemini 图像模型）也可以，会自动改用对话接口 |

配图也可以用图库：后台添加 Pexels 或 Unsplash 的 API Key 即可。

## 可选功能

### 联网检索

生成 PPT 和 Word 时可以勾选“联网检索资料”，按题目搜索并读取网页，引用处标注来源，Word 末尾附参考文献。需要一个 [SearXNG](https://github.com/searxng/searxng) 实例：

1. 在 SearXNG 的 `settings.yml` 中开启 JSON 输出：`search.formats` 加上 `json`
2. 在 `.env` 中填写 `DW_SEARXNG_URL`（地址写法见下文“访问宿主机或其他容器中的服务”），执行 `docker compose up -d`

不配置 SearXNG 时，仍然可以在生成页面直接填写参考网页。

### 定期备份到对象存储

在 `.env` 中填写 `DW_RESTIC_REPOSITORY` 和 `DW_RESTIC_PASSWORD`（以及对象存储需要的变量，见 [restic 文档](https://restic.readthedocs.io/)），每周自动用 restic 备份数据库和文件库。

## 使用域名和 HTTPS

在前面放任意一个反向代理即可，下面是两个例子。

**Caddy**（自动申请证书）：

```
doc.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

**Nginx**（证书可以用 Certbot 申请）：

```nginx
server {
    server_name doc.example.com;
    client_max_body_size 8m;          # 上传按 5MB 分片，留出余量

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
```

然后修改 `.env` 并执行 `docker compose up -d`：

- `DW_PUBLIC_URL=https://doc.example.com`
- `DW_COOKIE_SECURE=true`（通过 HTTPS 访问时必须开启；直接用 `http://` 访问时必须保持 `false`，否则无法登录）
- `DW_BIND=127.0.0.1`（只让反向代理访问，不直接对外开放端口）

登录限流按客户端 IP 计算。程序只在请求来自 `DW_TRUSTED_PROXIES`（默认是本机和 Docker 网段）时才读取代理传来的真实 IP。反向代理不在本机时，把它的地址加进去。

## 资源与并发

`.env` 中的设置：

| 配置项 | 作用 |
|---|---|
| `DW_PROFILE` | 预设档位：`4c24g`（默认）或 `2c12g`，决定下面三项的默认值 |
| `DW_HEAVY_GLOBAL` | 同时运行的重负载任务数（渲染、转换、OCR），`4c24g` 为 2，`2c12g` 为 1 |
| `DW_AI_CONCURRENCY` | 同时调用模型的任务数，`4c24g` 为 6，`2c12g` 为 3 |
| `DW_PROC_MEM_MB` | 单个外部处理程序（LibreOffice、OCR）的内存上限，`4c24g` 为 3072，`2c12g` 为 2048 |
| `MEM_*` | 各个容器的内存上限，`.env.example` 中给出了两档的参考值 |

内存紧张时，先把 `DW_HEAVY_GLOBAL` 设为 1，再按比例调低 `DW_PROC_MEM_MB` 和 `MEM_*`。处理的文件越大、页数越多，需要的内存越多。上传大小、页数等处理上限可以在后台“设置”中调整。

## 访问宿主机或其他容器中的服务

模型接口、SearXNG 等服务和 DocWork 在同一台机器上时：

- **服务映射了宿主机端口**：地址写 `http://host.docker.internal:端口`，前提是服务监听 `0.0.0.0` 或 Docker 网桥地址，而不是只监听 `127.0.0.1`
- **服务在另一个 Docker Compose 项目里**：新建 `docker-compose.override.yml`（Docker Compose 会自动合并它），让 `worker-ai` 加入那个网络，地址写 `http://容器名:端口`：

  ```yaml
  services:
    worker-ai:
      networks: [default, searxng]
  networks:
    searxng:
      external: true
      name: searxng_default   # docker network ls 查看实际名称
  ```

只有 `worker-ai` 需要访问外部服务；处理上传文件的容器没有网络。

## 常用命令

| 操作 | 命令 |
|---|---|
| 查看日志 | `docker compose logs -f web worker-ai` |
| 更新 | `git pull && docker compose build && docker compose up -d`（数据库结构自动升级） |
| 停止 | `docker compose down` |
| 重置管理员密码 | `docker compose run --rm -it web python -m app.cli reset-password` |
| 关闭两步验证 | `docker compose run --rm web python -m app.cli disable-totp` |
| 运行测试 | `docker compose run --rm web python -m unittest discover -s tests -p "test_*.py"` |

## 数据与备份

所有数据都在数据目录中（默认 `data/`，可以用 `DW_HOST_DATA` 改到别处）：数据库 `docwork.sqlite3`、文件库 `blobs/`。数据库每天自动备份到 `backups/`，保留 7 份。

迁移到新服务器：停止服务，把整个数据目录和 `.env` 复制过去，再启动。

## 安全说明

处理上传文件的程序（LibreOffice、Ghostscript、OCR）运行在单独的容器中：没有网络，根文件系统只读，去掉了全部特权，并且有内存和时间上限。如果 Docker 允许创建用户命名空间，还会用 bubblewrap 进一步隔离每个进程（`DW_SANDBOX=auto`）；不允许时会降级运行，并在后台“系统状态”中提示。

## 版本

更新记录见 [CHANGELOG.md](CHANGELOG.md)。
