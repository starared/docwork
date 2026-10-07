"""任务执行上下文：进度、取消检查、流式输出、子任务、临时目录、模型调用。"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from .. import jobs, storage
from ..config import get_settings
from ..llm import LLM

# 处理函数返回它表示“等待用户确认”（如大纲确认），任务暂停而不是完成。
AWAITING = object()


class Ctx:
    def __init__(self, job: dict):
        self.job = job
        self.id = job["id"]
        self.p = job["params"] or {}
        self.root = self.p.get("_root") or (self.id if not job.get("parent_id") else job.get("parent_id"))
        self.workspace_id = job["workspace_id"]
        self.token_id = job.get("token_id")
        self.work_id = job.get("work_id")
        self.tmp = get_settings().tmp_dir / f"job_{self.id}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self._last_stream = 0.0
        self._grant_checked = 0.0
        self.llm = LLM(dict(job, params=dict(self.p, _root=self.root)), self.check, self.stream)

    # ----- 控制 -----

    def check(self) -> None:
        if jobs.cancel_requested(self.id):
            raise jobs.Cancelled()
        # 访客任务：每 30 秒复查一次令牌授权（撤销、到期），失效后停止
        if self.token_id and time.time() - self._grant_checked > 30:
            self._grant_checked = time.time()
            from .. import accounts
            err = accounts.job_grant_error(self.token_id)
            if err:
                jobs.request_cancel(self.id, err + "，任务已停止")
                raise jobs.Cancelled()

    def cancelled(self) -> bool:
        return jobs.cancel_requested(self.id)

    def progress(self, p: float | None = None, stage: str | None = None, message: str | None = None) -> None:
        jobs.progress(self.id, p, stage, message)

    def stream(self, text: str) -> None:
        t = time.time()
        if t - self._last_stream > 0.3:
            jobs.set_stream(self.id, text)
            self._last_stream = t
            self.check()

    def sub_progress(self, lo: float, hi: float):
        def f(frac: float, msg: str = ""):
            self.progress(lo + (hi - lo) * max(0.0, min(1.0, frac)), message=msg or None)
            self.check()
        return f

    # ----- 子任务 -----

    def child(self, kind: str, step: str, params: dict, timeout: float = 1800) -> dict:
        """在对应的重负载队列上执行子任务并等待结果（受全局并发限制）。按 step 幂等。"""
        self.check()
        j = jobs.enqueue(kind, self.workspace_id, dict(params, _root=self.root), token_id=self.token_id,
                         work_id=self.work_id, parent_id=self.id, step=step, title=step)
        # 自己是重负载任务时，等待期间让出名额，否则子任务可能永远排不上（死锁）
        released = jobs.release_heavy(self.id)
        done = jobs.wait(j["id"], parent_id=self.id, timeout=timeout)
        if released:
            jobs.reacquire_heavy(self.id, self.check)
        return done["result"] or {}

    # ----- 文件 -----

    def materialize(self, file_or_sha, name: str | None = None, sub: str = "in") -> Path:
        return storage.materialize(file_or_sha, self.tmp / sub, name)

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
