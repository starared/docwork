"""常驻 LibreOffice：每个 worker 线程保留一个已启动的 soffice，通过 UNO 管道提交转换，省去每次 2 到 5 秒的冷启动。

- 只在 rlimit 沙箱下启用（DW_OFFICE_RESIDENT=auto）。bubblewrap 模式下每次转换各自隔离，常驻实例会跨任务
  处理文档，与这种模式的意图不符，所以仍走冷启动。
- 只处理来源格式明确、目标为 pdf/docx/pptx/xlsx 的转换；其他情况由 office.convert 走原来的冷启动。
- 任何失败（连不上、打开或保存失败、没有输出）都结束实例并返回 None，由调用方冷启动重试；连续失败 3 次后
  本进程停用常驻实例。超时和取消直接结束实例并报错，不再重试。
- 实例转换 MAX_USES 次、内存超过单进程上限、或空闲 IDLE_S 秒后结束，下次用到时重新启动。
- 每次转换先把源文件复制到实例自己的交换目录，输出也写在那里，完成后再移到任务目录。
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable

from ..config import get_settings
from . import sandbox

log = logging.getLogger(__name__)

HELPER = str(Path(__file__).with_name("uno_convert.py"))
# 按扩展名就能确定由哪个组件打开的来源格式（csv、txt、html 等交给冷启动的类型检测）
SRC_EXT = {".docx", ".doc", ".odt", ".rtf", ".pptx", ".ppt", ".odp", ".pps", ".ppsx", ".xlsx", ".xls", ".ods"}
TARGETS = {"pdf", "docx", "pptx", "xlsx"}
MAX_USES = 100
IDLE_S = 600
STARTUP_S = 40
MAX_FAILURES = 3

_lock = threading.Lock()
_instances: dict[int, "_Instance"] = {}
_failures = 0
_disabled = False
_reaper: threading.Thread | None = None


@lru_cache(maxsize=1)
def uno_python() -> str | None:
    py = get_settings().uno_python
    if not py or not Path(py).exists():
        return None
    try:
        r = subprocess.run([py, "-c", "import uno"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return py if r.returncode == 0 else None


def enabled() -> bool:
    s = get_settings()
    return (not _disabled and s.office_resident == "auto" and sandbox.effective_mode() == "rlimit"
            and uno_python() is not None)


def parse_filter(filt: str, out_ext: str) -> tuple[str, dict] | None:
    """'pdf:writer_pdf_Export:{json}' → ('writer_pdf_Export', {...})。带非 JSON 选项的过滤器不支持，返回 None。"""
    parts = filt.split(":", 2)
    if len(parts) < 2 or parts[0] != out_ext or out_ext not in TARGETS:
        return None
    data: dict = {}
    if len(parts) == 3 and parts[2]:
        if not parts[2].startswith("{"):
            return None
        try:
            data = json.loads(parts[2])
        except ValueError:
            return None
    return parts[1], data


class _Instance:
    def __init__(self, key: int):
        s = get_settings()
        self.dir = s.tmp_dir / f"lo_resident_{os.getpid()}_{key}"
        self.pipe = f"docwork_lo_{os.getpid()}_{key}"
        self.proc: subprocess.Popen | None = None
        self.uses = 0
        self.last = time.time()
        self.lock = threading.Lock()
        self.fresh = False

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        from .office import REGISTRY

        prof = self.dir / "profile"
        reg = prof / "user" / "registrymodifications.xcu"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(REGISTRY, encoding="utf-8")
        cmd = ["soffice", f"-env:UserInstallation=file://{prof}", "--headless", "--invisible", "--nologo", "--norestore",
               "--nodefault", "--nolockcheck", "--nofirststartwizard",
               f"--accept=pipe,name={self.pipe};urp;StarOffice.ComponentContext"]
        # CPU 时间是整个实例累计的，给足一天；实例会按次数和空闲时间回收
        full, env = sandbox.prepare(cmd, self.dir, cpu_s=86400)
        self.proc = subprocess.Popen(full, cwd=str(self.dir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, env=env, start_new_session=True)
        self.uses = 0
        self.fresh = True
        log.info("常驻 LibreOffice 已启动（pid %s）", self.proc.pid)

    def stop(self) -> None:
        if self.proc is not None:
            if self.proc.poll() is None:
                sandbox.kill(self.proc)
            self.proc = None

    def rss_mb(self) -> float:
        try:
            import psutil

            p = psutil.Process(self.proc.pid)
            return sum(x.memory_info().rss for x in [p, *p.children(recursive=True)]) / 1048576
        except Exception:
            return 0.0


def _instance() -> _Instance:
    global _reaper
    key = threading.get_ident()
    with _lock:
        inst = _instances.get(key)
        if inst is not None and inst.dir.parent != get_settings().tmp_dir:
            inst.stop()  # 数据目录换了（测试中会发生）：旧实例的目录已不再有效
            inst = None
        if inst is None:
            inst = _instances[key] = _Instance(key)
        if _reaper is None:
            _reaper = threading.Thread(target=_reap, name="lo-resident-reaper", daemon=True)
            _reaper.start()
    return inst


def _reap() -> None:
    while True:
        time.sleep(30)
        for inst in list(_instances.values()):
            if inst.alive() and time.time() - inst.last > IDLE_S and inst.lock.acquire(blocking=False):
                try:
                    log.info("常驻 LibreOffice 空闲超过 %d 秒，结束以释放内存", IDLE_S)
                    inst.stop()
                finally:
                    inst.lock.release()


@atexit.register
def shutdown() -> None:
    for inst in list(_instances.values()):
        try:
            inst.stop()
            shutil.rmtree(inst.dir, ignore_errors=True)
        except Exception:
            pass


def _failed(inst: _Instance, why: str) -> None:
    global _failures, _disabled
    inst.stop()
    _failures += 1
    log.warning("常驻 LibreOffice 转换失败（%s），改用冷启动", why)
    if _failures >= MAX_FAILURES:
        _disabled = True
        log.warning("常驻 LibreOffice 连续失败 %d 次，本进程停用，之后都使用冷启动", _failures)


def convert(src: Path, filt: str, out_ext: str, outdir: Path, *, cancel: Callable[[], bool] | None = None,
            timeout: int | None = None) -> Path | None:
    """成功返回输出文件；不适用或失败返回 None（调用方冷启动）。超时或取消时抛出异常。"""
    global _failures
    if src.suffix.lower() not in SRC_EXT:
        return None
    parsed = parse_filter(filt, out_ext)
    if parsed is None:
        return None
    name, data = parsed
    inst = _instance()
    with inst.lock:
        if not inst.alive():
            try:
                inst.start()
            except OSError as e:
                _failed(inst, f"无法启动：{e}")
                return None
        io = inst.dir / "io"
        shutil.rmtree(io, ignore_errors=True)
        (io / "in").mkdir(parents=True)
        (io / "out").mkdir()
        src_copy = io / "in" / src.name
        shutil.copyfile(src, src_copy)
        dst = io / "out" / f"{src.stem}.{out_ext}"
        wait = STARTUP_S if inst.fresh else 5
        args = [uno_python(), "-I", HELPER, inst.pipe, str(src_copy), str(dst), name, json.dumps(data) if data else "",
                str(wait)]
        try:
            r = sandbox.run(args, io, timeout=timeout, cancel=cancel, check=False)
        except (sandbox.ToolCancelled, sandbox.ToolError):
            # 实例可能卡在这个文件上：结束它，下次重新启动
            inst.stop()
            shutil.rmtree(io, ignore_errors=True)
            raise
        finally:
            inst.last = time.time()
        inst.fresh = False
        ok = r.code == 0 and dst.exists() and dst.stat().st_size > 0
        if not ok:
            shutil.rmtree(io, ignore_errors=True)
            if r.code in (4, 5) and inst.alive():
                # 打开或保存失败多半是文件本身的问题（损坏、加密），实例保留；冷启动会给出准确的错误
                log.info("常驻 LibreOffice 未能转换 %s（退出码 %s），改用冷启动重试", src.name, r.code)
            else:
                _failed(inst, f"退出码 {r.code}：{r.stderr.strip()[-300:]}")
            return None
        _failures = 0
        outdir.mkdir(parents=True, exist_ok=True)
        out = outdir / dst.name
        shutil.move(str(dst), str(out))
        shutil.rmtree(io, ignore_errors=True)
        inst.uses += 1
        limit = get_settings().proc_mem_limit_mb
        if inst.uses >= MAX_USES or not inst.alive() or inst.rss_mb() > limit:
            inst.stop()
        return out
