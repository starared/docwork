# DocWork 文档工作台

自托管的 AI 文档工作台：一个管理员账号加临时访问令牌，部署在一台服务器上。

## 功能

| 模块 | 说明 |
|---|---|
| AI 生成 PPT | 21 种布局、主题与配色约束、原生图表（可编辑数据）、原生表格、原生形状图标、配图（图库或图像生成）、演讲备注；可先确认大纲；渲染后自动检查溢出、截断、越界，两轮修正（精简文字时保护数字和限定词、拆页），剩余问题交给视觉模型复查或标记人工确认 |
| AI 生成 Word | 报告、方案、论文、纪要、说明书、公文 6 种预设；真实标题样式、目录域（两轮渲染写入页码）、原生公式（LaTeX 常用子集转 OMML，不支持的降级为图片）、原生图表（内嵌数据，可“编辑数据”）、表格表头跨页重复、题注编号、参考文献 |
| AI 生成 Excel | 有数据时模型只输出受限操作（筛选、分组、透视等），由 pandas 计算，合计行写成真实公式并用独立计算结果验证；没有数据时由模型设计工作簿；LibreOffice 在副本上重算检查错误值与引用范围 |
| 修改 | 对话修改（整份、选中页/元素/段落/单元格区域），只输出限定操作并检查范围；直接编辑；版本不可变（每次修改都生成新版本，可逐次恢复）、星标、对比、恢复；基于旧版本的修改会被拒绝 |
| 已有文件 | 导入 PPTX/DOCX/XLSX（旧格式先转换，宏被剥离），原位修改保留母版、版式、动画；DOCX 可用修订模式；写回前后比对元素清单；PDF 和图片可“重建”为可编辑的 PPT 或 Word |
| 格式转换 | Office ↔ PDF、PDF → Word / PPT（页面图片）/ Excel（表格）/ 图片 / 文字、图片 → PDF、Markdown/HTML → Word/PDF、Word → Markdown、CSV ↔ Excel，每种都标明保真等级 |
| PDF 工具 | 合并（保留书签）、拆分、压缩、旋转、排序与删页、提取文字（扫描页 OCR）、提取图片 |
| OCR | RapidOCR（中文）优先，Tesseract 备用；可搜索 PDF、Markdown 文字（含推断的表格）、Word；多栏阅读顺序；报告列出低置信度文字 |
| 访问控制 | 管理员账号（scrypt 密码哈希、可选两步验证、登录限流）；临时令牌（有效期、一次性、功能权限、配额、独立工作区、续发沿用工作区、撤销立即生效） |
| 管理 | 作品库（搜索、文件夹、标签、星标、回收站、打包下载）、任务中心、用量统计、系统状态、审计日志、预览字体、兼容性测试包 |

## 架构

```
浏览器 ──HTTPS──> 宿主机 Nginx ──> web（Starlette，仅监听 127.0.0.1）
                        │  下载：X-Accel-Redirect 直接发送文件
                        ▼
              /srv/docwork（容器内 /data）
              ├─ docwork.sqlite3   数据库 + 任务队列（WAL）
              ├─ blobs/            按内容哈希存储的文件库
              ├─ tmp/  backups/  fonts/
                        ▲
   worker-ai（调用模型，有外网）          scheduler（清理、备份、到期处理）
   worker-render / convert / ocr / preview（无网络、只读根文件系统）
```

- 任务状态保存在数据库中。认领任务时在同一个事务里检查全局重负载并发（4c24g 为 2，2c12g 为 1）；重负载任务等待自己的子任务（例如导入后生成预览）时暂时让出名额，避免死锁。取消是持久化标记，外部程序超时或取消时整组结束。子任务按步骤幂等，重试不会重复生成版本或重复扣配额。
- 访客的任务在执行前和执行中都会复查令牌：撤销或到期（且没有续发）后，排队和运行中的任务都会停止并结算配额。
- 历史版本超过保留期后删除预览图以释放空间，再次查看或对比该版本时自动重新生成。
- 配额在创建任务时原子预占，任务结束后按实际用量结算。
- 模型接口是 OpenAI 兼容的。接口只保存地址和密钥，模型从接口拉取后挑选添加；规划、写作、快速、视觉、图像生成五个角色由管理员分别指定模型（可以重复使用同一个模型），图库单独配置。未指定的角色对应功能降级。

## 部署（Ubuntu ARM64）

1. **安装 Docker**（已安装可跳过）

   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER   # 重新登录后生效
   ```

2. **准备目录和配置**

   ```bash
   cd ~/docwork                     # 解压后的项目目录
   cp .env.example .env
   openssl rand -hex 32             # 生成的值填到 .env 的 DW_MASTER_KEY
   nano .env                        # 至少填写 DW_MASTER_KEY、DW_PUBLIC_URL、DW_HOST_DATA
   sudo mkdir -p /srv/docwork && sudo chown 1000:1000 /srv/docwork && sudo chmod 755 /srv/docwork
   ```

3. **构建与自检**（第一次构建需要下载 LibreOffice、字体等，ARM 机器上约 10–20 分钟）

   ```bash
   docker compose build
   docker compose run --rm web python -m app.cli check
   docker compose run --rm -it web python -m app.cli create-owner
   docker compose up -d
   docker compose ps
   ```

4. **接入域名之前**：只通过 SSH 隧道访问。先在 `.env` 中临时设置 `DW_COOKIE_SECURE=false`、`DW_ACCEL_REDIRECT=false`，执行 `docker compose up -d`，然后在自己电脑上运行：

   ```bash
   ssh -L 8000:127.0.0.1:8000 ubuntu@服务器IP
   ```

   浏览器打开 http://127.0.0.1:8000 。

5. **接入域名与 HTTPS**（沿用服务器上已有的 Nginx 和 Certbot）

   ```bash
   sudo cp deploy/nginx-docwork.conf /etc/nginx/sites-available/docwork.conf
   sudo nano /etc/nginx/sites-available/docwork.conf   # 改域名，确认 /srv/docwork 路径
   sudo ln -s /etc/nginx/sites-available/docwork.conf /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot --nginx -d 你的域名
   ```

   把 `.env` 改回 `DW_COOKIE_SECURE=true`、`DW_ACCEL_REDIRECT=true`，并把 `DW_PUBLIC_URL` 设为 `https://你的域名`，执行 `docker compose up -d`。

   Nginx 需要能读取 `/srv/docwork/blobs`：容器写入的文件默认是所有人可读，目录权限为 755。如果下载的文件是空的或返回 404，先检查这个路径和权限，也可以临时把 `DW_ACCEL_REDIRECT` 设为 false，改由后端直接发送文件。

6. **配置模型**：用管理员账号登录 → 后台“模型接口” → “添加模型接口”（填写到 `/v1` 为止的地址和 API Key，例如 `http://new-api:3000/v1`）→ 保存后自动拉取接口的模型列表，勾选要用的模型（列表里没有的可以手动填写）→ 对模型点“测试对话”或“测试生图”了解它的能力 → 在“角色分配”中给规划、写作、快速、视觉、图像生成分别指定模型。模型不分类型，同一个模型可以负责多个角色（例如多模态模型同时做写作和视觉复查）。图库（Pexels 或 Unsplash 的 API Key）可选。

7. **自检**：分别生成一份 PPT、Word、Excel，各做一次格式转换和 OCR。然后在后台“设置”里生成兼容性测试包，下载到电脑上，用 Microsoft Office 和 WPS 逐一打开检查。

## 日常运维

| 操作 | 命令 |
|---|---|
| 查看日志 | `docker compose logs -f web worker-ai worker-render` |
| 更新 | 覆盖代码后 `docker compose build && docker compose up -d`（数据库结构自动迁移） |
| 切换资源配置档 | 修改 `.env` 中的 `DW_PROFILE` 和 `MEM_*`，执行 `docker compose up -d` |
| 重置管理员密码 | `docker compose run --rm -it web python -m app.cli reset-password` |
| 关闭两步验证 | `docker compose run --rm web python -m app.cli disable-totp` |
| 立即清理 | `docker compose run --rm web python -m app.cli maintenance expire_files clean_tmp gc_blobs` |
| 备份 | 数据库每天自动备份到 `/srv/docwork/backups`，保留 7 份；配置 `DW_RESTIC_*` 后每周用 restic 备份数据库和文件库。也可以直接用 rsync 同步整个 `/srv/docwork` |
| 恢复 | 停止服务，用备份覆盖 `/srv/docwork/docwork.sqlite3`（同时删除旁边的 `-wal`、`-shm` 文件），再启动 |
| 运行测试 | `docker compose run --rm web python -m unittest discover -s tests -p "test_*.py"` |

后台“设置”可调整处理上限（上传大小、解压后大小、页数、像素）和保留时间（工具结果、版本数量、回收站、工作区、预览图），以及磁盘告警阈值（超过 90% 时暂停上传和新任务）。

## 开发与测试

本地运行（需要 LibreOffice、Pandoc、Tesseract 等）：

```bash
pip install -r requirements.txt
export DW_DATA_DIR=./data DW_MASTER_KEY=dev-key-1234567890 DW_COOKIE_SECURE=false DW_ACCEL_REDIRECT=false
python -m app.cli create-owner
python -m uvicorn app.web.app:app --port 8000 &
python -m app.worker --queues ai,render,convert,ocr,preview --threads 4
```

测试：

- `tests/test_core.py`：账号、令牌、会话、任务队列（全局并发、取消、串行、心跳超时、父任务等待子任务不死锁）、令牌到期停止任务、配额（多线程并发预占）。
- `tests/test_tools.py`：Word 原生图表、文件检查（伪装类型、ZIP 炸弹、超大图片）、PDF 工具、全部转换、宏剥离、沙箱超时与取消。
- `tests/test_pipeline.py`：按 2 核配置（重负载并发 1）用模拟的模型接口跑通 PPT/Word/Excel 生成、大纲确认、对话修改（范围外不变、预览缓存）、版本冲突、直接编辑合并、恢复、导入与原位修改、PDF 重建、工具任务、取消。
- `tests/test_api.py`：登录与 CSRF、令牌隔离与越权（所有接口未登录都被拒绝；任务不能指向别人的作品）、一次性令牌、续发、分片上传续传、配额、下载、版本、回收站永久删除、历史预览清理与重新生成、搜索、后台。
- `tests/e2e_ui.py`：浏览器端到端测试（Playwright），覆盖登录、配置模型、生成、点选、直接编辑、对话修改、版本对比、令牌兑换、转换、PDF 工具、导入、Word 与 Excel 编辑器。

## 安全说明

**客户端地址与限流**：登录和令牌兑换按客户端 IP 限流。Uvicorn 以 `--no-proxy-headers` 运行，应用只在直连地址属于可信代理（`DW_TRUSTED_PROXIES`，默认本机和 Docker 网段）时读取 `X-Real-IP`。项目中的 Nginx 配置用 `$remote_addr` 覆盖 `X-Real-IP` 和 `X-Forwarded-For`，客户端自己伪造的转发头不会生效。如果你在 Nginx 前面还有 CDN 或另一层代理，需要相应调整这两处。

**沙箱**：处理上传文件的外部程序（LibreOffice、Ghostscript、OCR）运行在重负载容器中：没有网络、根文件系统只读、去掉全部权限、以普通用户运行、有内存和时间上限。在此之上，`DW_SANDBOX=auto` 会尝试用 bubblewrap 让每个外部程序只能写自己的任务目录。

Docker 默认的安全配置通常不允许容器内创建用户命名空间，所以 bubblewrap 在默认部署下**一般不可用**，会降级为“仅进程资源限制”。降级不是静默的：worker 日志有警告，后台“系统状态”显示每个重负载 worker 的实际沙箱方式。降级时的风险是：如果某个外部程序被恶意文档利用漏洞攻破，它可以读写数据目录（数据库和文件库），但无法联网把数据传出去。

如果要对外开放、希望更强隔离：
1. 先运行 `docker compose run --rm worker-convert python -m app.cli check`，查看 bubblewrap 是否可用。
2. 不可用时，需要为重负载容器放开用户命名空间（自定义 seccomp 配置；Ubuntu 24.04 宿主机还需要调整 AppArmor 的 `kernel.apparmor_restrict_unprivileged_userns`），这会改变宿主机的安全设置，请评估后再做。
3. 确认可用后设置 `DW_SANDBOX=bwrap`：之后 bubblewrap 一旦不可用，任务会直接失败，而不是降级运行。

## 已知限制

- 兼容性以“在约定版本的 Office、WPS 和约定字体环境下内容完整、无明显遮挡越界”为标准。服务器预览使用思源字体替代微软雅黑、宋体、仿宋等，版式以装有对应字体的电脑为准。
- PDF 转可编辑文件是“重建”，不保证还原原稿；扫描件的表格按版面规则推断，公式不识别。
- Word 与 PPT 中的瀑布图、桑基图等特殊图表以图片插入并附带 XLSX 数据，其余类型为原生图表。
- 原位修改 PPT 时，SmartArt、嵌入对象、动画原样保留，不提供修改。含图表的页面暂不支持“复制页”。
- 取消任务时会断开已发出的模型请求，但上游是否停止计费取决于接口。
- pdf2docx 已停止积极维护，作为可替换的首选后端；未安装或失败时使用内置重建。它依赖的 PyMuPDF 使用 AGPL 许可。
