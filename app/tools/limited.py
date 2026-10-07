"""在设置了资源限制的进程中执行外部程序。

由 sandbox.run 以 `python -I limited.py 内存MB CPU秒 文件MB -- 程序 参数...` 的方式调用：
先在这个（单线程的）子进程里设置 RLIMIT，再 exec 目标程序。worker 进程本身是多线程的，
不能在 Popen 的 preexec_fn 里做这些事（Python 文档：多线程程序中可能死锁）。
"""
from __future__ import annotations

import os
import resource
import sys

ENOENT_MARK = "DOCWORK_ENOENT"


def apply_limits(mem_mb: int, cpu_s: int, fsize_mb: int) -> None:
    try:
        if mem_mb:
            lim = mem_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
        if cpu_s:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 5))
        if fsize_mb:
            f = fsize_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (f, f))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass


def main(argv: list[str]) -> int:
    if len(argv) < 5 or argv[3] != "--":
        sys.stderr.write("用法：limited.py 内存MB CPU秒 文件MB -- 程序 参数...\n")
        return 2
    mem_mb, cpu_s, fsize_mb = (int(x) for x in argv[:3])
    cmd = argv[4:]
    apply_limits(mem_mb, cpu_s, fsize_mb)
    try:
        os.execvp(cmd[0], cmd)
    except FileNotFoundError:
        sys.stderr.write(f"{ENOENT_MARK} {cmd[0]}\n")
        return 127
    return 0  # exec 成功时不会执行到这里


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
