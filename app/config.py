"""运行配置。所有配置来自环境变量（.env），资源配置档决定并发和内存上限。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 两套资源配置档：并发数量。内存上限写在 docker-compose.yml 中，通过同名变量引用。
PROFILES = {
    "4c24g": {"heavy_global": 2, "ai_concurrency": 6, "lo_mem_mb": 3072, "ocr_mem_mb": 3072},
    "2c12g": {"heavy_global": 1, "ai_concurrency": 3, "lo_mem_mb": 2048, "ocr_mem_mb": 2048},
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DW_", env_file=".env", extra="ignore")

    data_dir: Path = Path("/data")
    profile: str = "4c24g"
    # 加密主密钥：用于加密存储的 API Key。部署时必须设置为随机值。
    master_key: str = Field(default="", description="32 字节以上随机字符串")
    # 对外访问地址（用于生成令牌链接），例如 https://doc.example.com
    public_url: str = "http://127.0.0.1:8000"
    # Cookie 是否带 Secure。接入 HTTPS 前的 SSH 隧道调试可设为 false。
    cookie_secure: bool = True
    # 由 Nginx 内部转发发送文件（X-Accel-Redirect）。本地开发可关闭。
    accel_redirect: bool = True
    accel_prefix: str = "/_protected"
    # 可信反向代理（逗号分隔的地址或网段）：只有直连地址在其中时才读取 X-Real-IP / X-Forwarded-For。
    # 默认是本机和 Docker 常用网段（宿主机 Nginx 经 Docker 网关连入容器）。
    trusted_proxies: str = "127.0.0.1/32,::1/128,172.16.0.0/12,192.168.0.0/16,10.0.0.0/8"
    # 沙箱模式：auto（能用 bubblewrap 就用）、bwrap、rlimit
    sandbox: str = "auto"
    heavy_global: int | None = None
    ai_concurrency: int | None = None
    # 单个外部进程（LibreOffice、OCR）的内存上限，MB
    proc_mem_mb: int | None = None
    # 任务超时（秒）
    heavy_timeout: int = 600
    ai_timeout: int = 1800
    # 处理上限默认值（后台可调整）
    max_upload_mb: int = 200
    max_unzipped_mb: int = 1024
    max_pages: int = 1000
    max_pixels: int = 100_000_000
    max_job_tmp_mb: int = 5120
    # 登录与兑换限流
    login_rate_per_10min: int = 10
    redeem_rate_per_10min: int = 20
    # 可选：restic 备份仓库（留空则只做本地数据库备份）
    restic_repository: str = ""
    restic_password: str = ""
    # 调试：允许非 HTTPS 下 Cookie 生效、打印详细错误
    debug: bool = False

    @property
    def prof(self) -> dict:
        return PROFILES.get(self.profile, PROFILES["4c24g"])

    @property
    def heavy_limit(self) -> int:
        return self.heavy_global or self.prof["heavy_global"]

    @property
    def ai_limit(self) -> int:
        return self.ai_concurrency or self.prof["ai_concurrency"]

    @property
    def proc_mem_limit_mb(self) -> int:
        return self.proc_mem_mb or self.prof["lo_mem_mb"]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "docwork.sqlite3"

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def font_dir(self) -> Path:
        return self.data_dir / "fonts"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.blob_dir, self.tmp_dir, self.upload_dir, self.backup_dir, self.font_dir,
                  self.tmp_dir / "mpl", self.tmp_dir / "cache", self.tmp_dir / "home"):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


def reset_settings() -> None:
    """测试用：清空缓存以重新读取环境变量。"""
    get_settings.cache_clear()


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR.parent / "static"
RENDERER_VERSION = "1.0.0"
SPEC_VERSION = 1
