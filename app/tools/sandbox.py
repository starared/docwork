"""外部程序（LibreOffice、Ghostscript、OCR、Pandoc）的受控执行。

- 独立进程组：取消或超时时结束整个进程组。
- 资源限制：虚拟内存、CPU 时间、单文件大小（RLIMIT），由单线程的 limited.py 在 exec 目标程序前设置
  （worker 是多线程进程，不使用 Popen 的 preexec_fn）。
- 可选 bubblewrap：无网络、系统只读、只能写任务目录（需要容器允许用户命名空间；不可用时自动退回）。
- 生产部署中，重负载 worker 容器本身位于无外网的内部网络，且根文件系统只读。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Sequence

from ..config import get_settings


class ToolError(RuntimeError):
    pass


class ToolCancelled(RuntimeError):
    pass


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    seconds: float


@lru_cache(maxsize=1)
def bwrap_available() -> bool:
    if not shutil.which("bwrap"):
        return False
    try:
        r = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-net", "true"],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def effective_mode() -> str:
    """实际生效的隔离方式：bwrap（独立命名空间，只能写任务目录）或 rlimit（仅进程资源限制）。"""
    mode = get_settings().sandbox
    if mode in ("auto", "bwrap") and bwrap_available():
        return "bwrap"
    if mode == "bwrap":
        return "unavailable"
    return "rlimit"


_LIMITED = str(Path(__file__).with_name("limited.py"))


def _limited(mem_mb: int, cpu_s: int, fsize_mb: int, cmd: Sequence[str]) -> list[str]:
    """用 limited.py 包一层：在单线程子进程里设置 RLIMIT 后 exec 目标程序。"""
    return [sys.executable, "-I", _LIMITED, str(mem_mb), str(cpu_s), str(fsize_mb), "--", *cmd]


def run(
    cmd: Sequence[str],
    cwd: Path,
    *,
    timeout: int | None = None,
    cancel: Callable[[], bool] | None = None,
    mem_mb: int | None = None,
    network: bool = False,
    env: dict | None = None,
    writable: Sequence[Path] = (),
    check: bool = True,
) -> Result:
    s = get_settings()
    timeout = timeout or s.heavy_timeout
    # 虚拟内存上限取进程内存上限的 3 倍：LibreOffice 等程序会预留大量虚拟地址空间，
    # 实际物理内存由容器内存上限约束，这里只防止失控进程。
    mem = (mem_mb or s.proc_mem_limit_mb) * 3
    full = _limited(mem, timeout + 30, s.max_job_tmp_mb, cmd)
    mode = s.sandbox
    if mode in ("auto", "bwrap") and bwrap_available():
        wr = [str(Path(cwd).resolve())] + [str(Path(p).resolve()) for p in writable]
        pre = ["bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
        for w in wr:
            pre += ["--bind", w, w]
        if not network:
            pre += ["--unshare-net"]
        pre += ["--unshare-pid", "--unshare-ipc", "--chdir", str(Path(cwd).resolve())]
        full = pre + full
    elif mode == "bwrap":
        raise ToolError("配置要求使用 bubblewrap 沙箱，但当前环境不可用")
    penv = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "TMPDIR": str(cwd), "SAL_USE_VCLPLUGIN": "svp"}
    if env:
        penv.update(env)
    t0 = time.time()
    out_f = Path(cwd) / f".stdout.{os.getpid()}.{int(t0 * 1000)}"
    err_f = Path(cwd) / f".stderr.{os.getpid()}.{int(t0 * 1000)}"
    with open(out_f, "wb") as fo, open(err_f, "wb") as fe:
        try:
            p = subprocess.Popen(full, cwd=str(cwd), stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, env=penv,
                                 start_new_session=True)
        except FileNotFoundError:
            raise ToolError(f"找不到程序：{full[0]}")
        reason = None
        while True:
            try:
                p.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel and cancel():
                reason = "cancel"
            elif time.time() - t0 > timeout:
                reason = "timeout"
            if reason:
                _kill(p)
                break
    stdout = out_f.read_text(errors="replace")[-20000:]
    stderr = err_f.read_text(errors="replace")[-20000:]
    out_f.unlink(missing_ok=True)
    err_f.unlink(missing_ok=True)
    dt = time.time() - t0
    if reason == "cancel":
        raise ToolCancelled("已取消")
    if reason == "timeout":
        raise ToolError(f"{Path(cmd[0]).name} 运行超时（{timeout} 秒），已结束进程")
    if p.returncode == 127 and "DOCWORK_ENOENT" in stderr:
        raise ToolError(f"找不到程序：{cmd[0]}")
    if check and p.returncode != 0:
        raise ToolError(f"{Path(cmd[0]).name} 执行失败（退出码 {p.returncode}）：{(stderr or stdout)[-800:]}")
    return Result(p.returncode, stdout, stderr, dt)


def _kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait(timeout=5)


def which(name: str) -> str | None:
    return shutil.which(name)
