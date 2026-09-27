# DocWork 文档工作台

自托管的 AI 文档工作台：一个管理员账号加临时访问令牌，部署在一台服务器上。

## 环境要求

- Linux 服务器，已安装 Docker 和 Docker Compose 插件
- 内存建议 24GB（最低 12GB，见下方“资源配置”）
- 一个 OpenAI 兼容格式的模型接口（地址 + API Key）

## 部署

1. **获取代码**

   ```bash
   git clone https://github.com/starared/docwork.git
   cd docwork
   ```

2. **填写配置**

   ```bash
   cp .env.example .env
   openssl rand -hex 32        # 把输出填到 .env 的 DW_MASTER_KEY
   nano .env
   ```

   至少需要修改：

   | 配置项 | 说明 |
   |---|---|
   | `DW_MASTER_KEY` | 加密保存 API Key 用的主密钥，填上一步生成的值。设置后不要再改 |
   | `DW_PUBLIC_URL` | 浏览器实际访问的地址，例如 `http://服务器IP:8000` |
   | `DW_HOST_DATA` | 宿主机上的数据目录，默认 `/srv/docwork` |
   | `DW_BIND` / `DW_PORT` | 监听地址和端口，默认 `0.0.0.0:8000` |
   | `DW_COOKIE_SECURE` | 用 `http://` 访问时保持 `false`；通过 HTTPS 访问时改为 `true` |

3. **创建数据目录**

   ```bash
   sudo mkdir -p /srv/docwork
   sudo chown 1000:1000 /srv/docwork
   ```

   目录要和 `.env` 里的 `DW_HOST_DATA` 一致。

4. **构建并启动**

   第一次构建需要下载 LibreOffice、字体等，约 10–20 分钟。

   ```bash
   docker compose build
   docker compose run --rm web python -m app.cli check                # 环境自检
   docker compose run --rm -it web python -m app.cli create-owner      # 创建管理员账号
   docker compose up -d
   docker compose ps
   ```

5. **访问**

   浏览器打开 `DW_PUBLIC_URL` 对应的地址，用刚创建的管理员账号登录。服务器有防火墙或云安全组时，需要放行 `DW_PORT` 端口。

6. **配置模型**

   后台 → “模型接口” → “添加模型接口”：

   - **接口地址**：OpenAI 兼容接口，填写到 `/v1` 为止，例如 `https://api.openai.com/v1`。接口服务运行在同一台宿主机上时写 `http://host.docker.internal:端口/v1`
   - **API Key**：接口的密钥

   保存后点“拉取模型”，勾选要用的模型（列表里没有的可以手动填写），然后在“角色分配”中给规划、写作、快速、视觉、图像生成分别指定模型。同一个模型可以担任多个角色，未指定的角色对应功能不可用。

   接口需要支持 `/v1/chat/completions`；图像生成角色需要支持 `/v1/images/generations`；视觉角色需要模型支持图片输入。

## 资源配置

`.env` 中的 `DW_PROFILE` 和 `MEM_*` 按服务器配置选择：

- `4c24g`（4 核 24GB，默认）
- `2c12g`（2 核 12GB）：把 `.env` 里注释掉的那组值取消注释，替换默认值

修改后执行 `docker compose up -d` 生效。

## 常用命令

| 操作 | 命令 |
|---|---|
| 查看日志 | `docker compose logs -f web worker-ai` |
| 更新 | `git pull && docker compose build && docker compose up -d`（数据库结构自动迁移） |
| 停止 | `docker compose down` |
| 重置管理员密码 | `docker compose run --rm -it web python -m app.cli reset-password` |
| 关闭两步验证 | `docker compose run --rm web python -m app.cli disable-totp` |

## 数据与备份

所有数据都在 `DW_HOST_DATA` 目录中（数据库 `docwork.sqlite3` 和文件库 `blobs/`）。数据库每天自动备份到该目录下的 `backups/`，保留 7 份。

迁移到新服务器：停止服务，把整个数据目录和 `.env` 复制过去，再按上面的步骤启动。
