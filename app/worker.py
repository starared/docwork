"""任务 worker：从数据库队列认领任务并执行。

用法：python -m app.worker --queues ai --threads 6
      python -m app.worker --queues render,convert,ocr,preview --threads 2
重负载任务的全局并发由认领时的数据库事务保证，与启动了多少个 worker 无关。
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import time
import traceback

from . import accounts, db, jobs, quota
from .config import get_settings
from .util import UserError

log = logging.getLogger("docwork.worker")


def handlers() -> dict:
    from .pipeline import artifacts, common, doc, edit, importer, ppt, toolsjobs, xls

    return {
        "gen_ppt": ppt.handle_gen_ppt,
        "gen_doc": doc.handle_gen_doc,
        "gen_xls": xls.handle_gen_xls,
        "edit": edit.handle_edit,
        "manual_edit": edit.handle_manual_edit,
        "import_edit": importer.handle_import_edit,
        "rebuild": importer.handle_rebuild,
        "model_test": toolsjobs.handle_model_test,
        "render": artifacts.handle_render,
        "compat_pack": toolsjobs.handle_compat_pack,
        "convert": toolsjobs.handle_convert,
        "pdf_tool": toolsjobs.handle_pdf_tool,
        "import_file": importer.handle_import_file,
        "extract": common.handle_extract,
        "ocr": toolsjobs.handle_ocr,
        "preview": importer.handle_preview,
        "file_pages": toolsjobs.handle_file_pages,
        "version_preview": importer.handle_version_preview,
    }


def execute(job: dict, table: dict | None = None) -> None:
    """执行一个已认领的任务，负责完成、失败、取消与配额结算。"""
    from .pipeline.context import Ctx
    from .pipeline.ppt import AWAITING
    from .tools.sandbox import ToolCancelled, ToolError
    from .llm import LLMError

    table = table or handlers()
    ctx = Ctx(job)
    stop = threading.Event()

    def beat():
        while not stop.wait(20):
            try:
                jobs.heartbeat(job["id"])
            except Exception:
                pass
        db.close_thread_conn()
    hb = threading.Thread(target=beat, daemon=True)
    hb.start()
    try:
        fn = table.get(job["kind"])
        if fn is None:
            raise UserError(f"未知任务类型：{job['kind']}")
        if jobs.cancel_requested(job["id"]):
            raise jobs.Cancelled()
        err = accounts.job_grant_error(job.get("token_id"))
        if err:
            jobs.request_cancel(job["id"], err + "，任务未执行")
            raise jobs.Cancelled()
        result = fn(ctx)
        if result is AWAITING:
            return
        jobs.finish(job["id"], result)
    except (jobs.Cancelled, ToolCancelled):
        jobs.mark_cancelled(job["id"])
    except UserError as e:
        jobs.fail(job["id"], e.message)
    except (LLMError, ToolError) as e:
        jobs.fail(job["id"], str(e))
    except Exception as e:  # 未预期的错误：记录详细信息，允许重试一次
        log.error("任务 %s 失败：%s", job["id"], traceback.format_exc())
        msg = f"{type(e).__name__}: {e}"
        jobs.fail(job["id"], msg if not get_settings().debug else msg + "\n" + traceback.format_exc()[-3000:],
                  retry=job["kind"] in ("render", "convert", "preview", "extract", "pdf_tool", "ocr"))
    finally:
        stop.set()
        cur = jobs.get(job["id"])
        if cur and cur["status"] in jobs.FINAL and not job.get("parent_id"):
            try:
                quota.settle(job["id"])
            except Exception:
                log.exception("配额结算失败")
            if cur["work_id"]:
                db.run("UPDATE works SET busy_job_id=NULL WHERE id=? AND busy_job_id=?", (cur["work_id"], job["id"]))
        if cur and cur["status"] in jobs.FINAL:
            ctx.cleanup()


class Worker:
    def __init__(self, queues: list[str], threads: int):
        self.queues = queues
        self.threads = threads
        self.name = f"{socket.gethostname()}:{os.getpid()}"
        self.stopping = threading.Event()
        self.table = handlers()

    def loop(self, n: int):
        idle = 0.5
        while not self.stopping.is_set():
            try:
                job = jobs.claim(self.queues, f"{self.name}/{n}", os.getpid())
            except Exception:
                log.exception("认领任务失败")
                job = None
            if job is None:
                self.stopping.wait(idle)
                idle = min(2.0, idle * 1.3)
                continue
            idle = 0.3
            log.info("开始任务 %s (%s)", job["id"], job["kind"])
            execute(job, self.table)
            log.info("结束任务 %s", job["id"])
        db.close_thread_conn()

    def run(self):
        db.migrate()
        ts = [threading.Thread(target=self.loop, args=(i,), daemon=True) for i in range(self.threads)]
        for t in ts:
            t.start()

        def stop(*_):
            log.info("收到停止信号，等待当前任务结束……")
            self.stopping.set()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while any(t.is_alive() for t in ts):
            time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queues", default="ai")
    ap.add_argument("--threads", type=int, default=0)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = get_settings()
    queues = [q.strip() for q in a.queues.split(",") if q.strip()]
    threads = a.threads or (s.ai_limit if queues == ["ai"] else s.heavy_limit)
    log.info("worker 启动：队列 %s，线程 %d", queues, threads)
    if any(q in jobs.HEAVY_QUEUES for q in queues):
        # 重负载 worker 启动时报告实际生效的沙箱，后台“系统状态”显示；auto 模式降级不再是静默的
        from .tools import sandbox
        eff = sandbox.effective_mode()
        if eff == "bwrap":
            log.info("沙箱：bubblewrap（外部程序只能写各自的任务目录）")
        elif eff == "rlimit":
            log.warning("沙箱：bubblewrap 在此容器中不可用，已降级为进程资源限制。外部程序（LibreOffice、Ghostscript、OCR）"
                        "仍受容器隔离（无网络、根文件系统只读、无特权），但可以读写 /data。需要更强隔离请设置 DW_SANDBOX=bwrap 并按 README 启用。")
        else:
            log.error("沙箱：DW_SANDBOX=bwrap 但 bubblewrap 不可用，所有调用外部程序的任务都会失败")
        try:
            db.set_setting(f"sandbox_effective:{','.join(queues)}", {"mode": eff, "configured": s.sandbox, "at": time.time()})
        except Exception:
            log.exception("记录沙箱状态失败")
    Worker(queues, threads).run()


if __name__ == "__main__":
    main()
