"""运行配置。所有配置来自环境变量（.env），资源配置档决定并发和内存上限。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 资源配置档：决定并发数量和单个外部进程（LibreOffice、OCR）的内存封顶值。
# 这些都是上限，不是常驻占用；各容器的内存封顶值写在 docker-compose.yml 中，通过 MEM_* 变量引用。
# 单项可用 DW_HEAVY_GLOBAL、DW_AI_CONCURRENCY、DW_PROC_MEM_MB 覆盖。
PROFILES = {
    "standard": {"heavy_global": 2, "ai_concurrency": 4, "lo_mem_mb": 2560, "ocr_mem_mb": 2560},
    "large": {"heavy_global": 2, "ai_concurrency": 6, "lo_mem_mb": 3072, "ocr_mem_mb": 3072},
    "small": {"heavy_global": 1, "ai_concurrency": 3, "lo_mem_mb": 2048, "ocr_mem_mb": 2048},
}
# 旧名称（1.1.1 之前的 .env 可能还在用）
PROFILE_ALIASES = {"4c10g": "standard", "4c24g": "large", "2c12g": "small"}
DEFAULT_PROFILE = "standard"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DW_", env_file=".env", extra="ignore")

    data_dir: Path = Path("/data")
    profile: str = DEFAULT_PROFILE
    # 加密主密钥：用于加密存储的 API Key。部署时必须设置为随机值。
    master_key: str = Field(default="", description="32 字节以上随机字符串")
    # 对外访问地址（用于生成令牌链接），例如 https://doc.example.com
    public_url: str = "http://127.0.0.1:8000"
    # Cookie 是否带 Secure。接入 HTTPS 前的 SSH 隧道调试可设为 false。
    cookie_secure: bool = True
    # 由前置 Nginx 内部转发发送文件（X-Accel-Redirect）。默认关闭，由程序直接发送文件。
    accel_redirect: bool = False
    accel_prefix: str = "/_protected"
    # 可信反向代理（逗号分隔的地址或网段）：只有直连地址在其中时才读取 X-Real-IP / X-Forwarded-For。
    # 默认只有本机和 Docker 网段（宿主机 Nginx 经 Docker 网关连入容器）。不包含 10.0.0.0/8 和 192.168.0.0/16：
    # 直接部署在局域网时这些是普通用户的地址，信任它们会让局域网用户伪造来源地址绕过登录限流。
    trusted_proxies: str = "127.0.0.1/32,::1/128,172.16.0.0/12"
    # 沙箱模式：auto（能用 bubblewrap 就用）、bwrap、rlimit
    sandbox: str = "auto"
    heavy_global: int | None = None
    ai_concurrency: int | None = None
    # 单个外部进程（LibreOffice、OCR）的内存上限，MB
    proc_mem_mb: int | None = None
    # 常驻 LibreOffice：auto（只在 rlimit 沙箱下、且系统 Python 能导入 uno 时启用）或 off。
    # 常驻实例省去每次转换 2 到 5 秒的冷启动，但一个实例会先后处理多个任务的文档。
    office_resident: str = "auto"
    # 运行 UNO 转换脚本的 Python（需要能 import uno，Debian/Ubuntu 上由 python3-uno 提供）
    uno_python: str = "/usr/bin/python3"
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
    # 可选：SearXNG 地址（例如 http://searxng:8080，需开启 JSON 输出），用于生成时联网检索资料
    searxng_url: str = ""
    # 联网检索时最多读取的网页数量
    web_pages: int = 6
    # 可选：restic 备份仓库（留空则只做本地数据库备份）
    restic_repository: str = ""
    restic_password: str = ""
    # 调试：允许非 HTTPS 下 Cookie 生效、打印详细错误
    debug: bool = False

    @property
    def profile_name(self) -> str:
        name = PROFILE_ALIASES.get(self.profile, self.profile)
        return name if name in PROFILES else DEFAULT_PROFILE

    @property
    def prof(self) -> dict:
        return PROFILES[self.profile_name]

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
# 程序版本（发布时同步打 Git 标签 v版本号）
APP_VERSION = "1.1.1"
RENDERER_VERSION = "1.0.0"
SPEC_VERSION = 1
