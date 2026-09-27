"""命令行：创建管理员账号、重置密码、关闭两步验证、执行维护任务。

  python -m app.cli create-owner            （交互输入用户名和密码）
  python -m app.cli reset-password
  python -m app.cli disable-totp
  python -m app.cli maintenance [任务名...]
  python -m app.cli check                   （检查运行环境）
"""
from __future__ import annotations

import getpass
import json
import shutil
import sys

from . import accounts, db
from .config import get_settings
from .util import UserError


def main(argv: list[str]) -> int:
    db.migrate()
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    try:
        if cmd == "create-owner":
            user = input("管理员用户名：").strip() or "admin"
            pw = getpass.getpass("密码（至少 10 位）：")
            if pw != getpass.getpass("再次输入密码："):
                print("两次输入不一致")
                return 1
            accounts.create_owner(user, pw)
            print("管理员账号已创建")
        elif cmd == "reset-password":
            pw = getpass.getpass("新密码（至少 10 位）：")
            if pw != getpass.getpass("再次输入："):
                print("两次输入不一致")
                return 1
            accounts.reset_owner_password(pw)
            print("密码已重置，已有登录会话全部失效")
        elif cmd == "disable-totp":
            db.run("UPDATE owner SET totp_enabled=0, totp_secret=NULL WHERE id=1")
            print("两步验证已关闭")
        elif cmd == "maintenance":
            from . import scheduler
            print(json.dumps(scheduler.run_once(argv[1:] or None), ensure_ascii=False, default=str, indent=1))
        elif cmd == "check":
            return check()
        else:
            print(__doc__)
            return 1
    except UserError as e:
        print("错误：", e.message)
        return 1
    return 0


def check() -> int:
    s = get_settings()
    ok = True
    print("数据目录：", s.data_dir)
    print("资源配置档：", s.profile, f"（重负载并发 {s.heavy_limit}，AI 并发 {s.ai_limit}）")
    if not s.master_key or len(s.master_key) < 16:
        print("× 未设置 DW_MASTER_KEY")
        ok = False
    for tool in ("soffice", "gs", "pandoc", "fc-match", "qpdf"):
        print(("✓ " if shutil.which(tool) else "× ") + tool)
        ok = ok and bool(shutil.which(tool))
    from .tools import ocr, sandbox
    print("OCR 引擎：", ocr.engine_name() or "无")
    print("bubblewrap 沙箱：", "可用" if sandbox.bwrap_available() else "不可用（使用进程资源限制与无外网容器）")
    from .render.fonts import font_file
    for fam in ("微软雅黑", "宋体", "Arial"):
        print(f"字体 {fam} → {font_file(fam)}")
    try:
        import pdf2docx  # noqa: F401
        print("✓ pdf2docx")
    except ImportError:
        print("- pdf2docx 未安装（使用内置重建后端）")
    print("管理员账号：", "已创建" if db.one("SELECT id FROM owner WHERE id=1") else "未创建（运行 create-owner）")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
