"""SQLite 数据访问。

- WAL 模式，允许 Web 与多个 worker 进程并发读写。
- 需要原子性的操作（任务认领、配额预占、令牌兑换）使用 BEGIN IMMEDIATE，
  在事务开始时即取得写锁，相当于 PostgreSQL 中的 SELECT ... FOR UPDATE。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import get_settings

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

    CREATE TABLE owner (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        username TEXT NOT NULL,
        pw_hash TEXT NOT NULL,
        totp_secret TEXT,
        totp_enabled INTEGER NOT NULL DEFAULT 0,
        workspace_id TEXT NOT NULL,
        created_at REAL NOT NULL
    );

    CREATE TABLE workspaces (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,              -- owner | guest
        delete_after REAL,               -- 计划删除时间（最后一个有效授权结束 + 保留期）
        created_at REAL NOT NULL,
        deleted_at REAL
    );

    CREATE TABLE tokens (
        id TEXT PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        note TEXT NOT NULL DEFAULT '',
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        expires_at REAL NOT NULL,
        one_time INTEGER NOT NULL DEFAULT 0,
        redeemed_at REAL,                -- 兑换状态
        revoked_at REAL,                 -- 授权状态（与到期共同决定）
        perms TEXT NOT NULL DEFAULT '[]',
        quota TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    );
    CREATE INDEX tokens_ws ON tokens(workspace_id);

    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,             -- Cookie 值的 SHA-256
        kind TEXT NOT NULL,              -- owner | guest
        token_id TEXT REFERENCES tokens(id),
        workspace_id TEXT NOT NULL,
        csrf TEXT NOT NULL,
        created_at REAL NOT NULL,
        last_seen REAL NOT NULL,
        expires_at REAL NOT NULL
    );
    CREATE INDEX sessions_token ON sessions(token_id);

    CREATE TABLE blobs (
        sha TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        created_at REAL NOT NULL
    );

    CREATE TABLE files (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        sha TEXT NOT NULL,
        name TEXT NOT NULL,
        mime TEXT NOT NULL,
        size INTEGER NOT NULL,
        kind TEXT NOT NULL,              -- upload | output | export | asset | preview | original | attachment
        work_id TEXT,
        version_id TEXT,
        job_id TEXT,
        meta TEXT NOT NULL DEFAULT '{}',
        expires_at REAL,
        created_at REAL NOT NULL,
        deleted_at REAL
    );
    CREATE INDEX files_ws ON files(workspace_id, kind);
    CREATE INDEX files_sha ON files(sha);
    CREATE INDEX files_job ON files(job_id);

    CREATE TABLE works (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        kind TEXT NOT NULL,              -- ppt | doc | xls | import_pptx | import_docx | import_xlsx
        title TEXT NOT NULL,
        folder TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '[]',
        starred INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'generate',
        current_version_id TEXT,
        origin_file_id TEXT,
        shared_from TEXT,
        busy_job_id TEXT,                -- 正在运行的修改任务（同一作品同时只运行一个）
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        deleted_at REAL
    );
    CREATE INDEX works_ws ON works(workspace_id, deleted_at, updated_at);

    CREATE VIRTUAL TABLE works_fts USING fts5(work_id UNINDEXED, title, body, tokenize='trigram');

    CREATE TABLE shares (
        work_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        mode TEXT NOT NULL,              -- readonly
        created_at REAL NOT NULL,
        PRIMARY KEY (work_id, workspace_id)
    );

    CREATE TABLE versions (
        id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL,
        number INTEGER NOT NULL,
        job_id TEXT UNIQUE,              -- 幂等：同一任务只产生一个版本
        source TEXT NOT NULL,            -- generate | chat | manual | import | restore | inplace
        message TEXT NOT NULL DEFAULT '',
        spec TEXT,                       -- AI 作品的规格（JSON）
        spec_version INTEGER,
        renderer_version TEXT,
        file_sha TEXT,                   -- 导入作品：该版本的文件
        manifest TEXT NOT NULL DEFAULT '{}',
        changed TEXT NOT NULL DEFAULT '[]',
        starred INTEGER NOT NULL DEFAULT 0,
        base_version_id TEXT,
        created_at REAL NOT NULL,
        UNIQUE (work_id, number)
    );

    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        parent_id TEXT,
        step TEXT,
        kind TEXT NOT NULL,
        queue TEXT NOT NULL,
        heavy INTEGER NOT NULL DEFAULT 0,
        workspace_id TEXT NOT NULL,
        token_id TEXT,
        work_id TEXT,
        title TEXT NOT NULL DEFAULT '',
        params TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL,            -- queued running awaiting_input cancelling cancelled done failed
        progress REAL NOT NULL DEFAULT 0,
        stage TEXT NOT NULL DEFAULT '',
        message TEXT NOT NULL DEFAULT '',
        result TEXT,
        error TEXT,
        stream TEXT NOT NULL DEFAULT '',
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 2,
        worker TEXT,
        pid INTEGER,
        heartbeat_at REAL,
        created_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL,
        updated_at REAL NOT NULL,
        UNIQUE (parent_id, step)
    );
    CREATE INDEX jobs_claim ON jobs(status, queue, created_at);
    CREATE INDEX jobs_ws ON jobs(workspace_id, created_at);
    CREATE INDEX jobs_parent ON jobs(parent_id);

    CREATE TABLE usage (
        id TEXT PRIMARY KEY,
        job_id TEXT,
        root_job_id TEXT,
        workspace_id TEXT,
        token_id TEXT,
        endpoint_id TEXT,
        model TEXT,
        role TEXT,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        images INTEGER NOT NULL DEFAULT 0,
        estimated INTEGER NOT NULL DEFAULT 0,
        cost REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );
    CREATE INDEX usage_root ON usage(root_job_id);
    CREATE INDEX usage_token ON usage(token_id, created_at);

    CREATE TABLE reservations (
        job_id TEXT PRIMARY KEY,
        token_id TEXT NOT NULL,
        gen_count INTEGER NOT NULL DEFAULT 0,
        tokens INTEGER NOT NULL DEFAULT 0,
        storage_bytes INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,            -- reserved | settled
        actual_tokens INTEGER,
        actual_gen INTEGER,
        created_at REAL NOT NULL,
        settled_at REAL
    );
    CREATE INDEX reservations_token ON reservations(token_id, status);

    CREATE TABLE endpoints (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,              -- openai | stock（v3 之前为 text | vision | image | stock）
        base_url TEXT NOT NULL,
        api_key_enc TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        extra TEXT NOT NULL DEFAULT '{}',
        capabilities TEXT NOT NULL DEFAULT '{}',
        price_in REAL NOT NULL DEFAULT 0,   -- 每百万输入 token 价格
        price_out REAL NOT NULL DEFAULT 0,  -- 每百万输出 token 价格
        price_image REAL NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL
    );

    CREATE TABLE audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '{}',
        ip TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX audit_ts ON audit(ts);

    CREATE TABLE uploads (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        name TEXT NOT NULL,
        size INTEGER NOT NULL,
        chunk_size INTEGER NOT NULL,
        received TEXT NOT NULL DEFAULT '[]',
        created_at REAL NOT NULL
    );

    CREATE TABLE rate (
        key TEXT NOT NULL,
        ts REAL NOT NULL
    );
    CREATE INDEX rate_key ON rate(key, ts);
    """,
    # v2：每个版本保留自己的正文索引，恢复和复制不再丢失搜索内容。
    """ALTER TABLE versions ADD COLUMN search_text TEXT;""",
    # v3：接口与模型分开。接口只保存地址和密钥（OpenAI 兼容或图库），模型从接口拉取后逐个添加，
    # 不再按“文字/视觉/生图”给接口分类，由管理员在角色分配中决定每个模型负责什么。
    """
    CREATE TABLE models (
        id TEXT PRIMARY KEY,
        endpoint_id TEXT NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
        model TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        extra TEXT NOT NULL DEFAULT '{}',
        capabilities TEXT NOT NULL DEFAULT '{}',
        price_in REAL NOT NULL DEFAULT 0,
        price_out REAL NOT NULL DEFAULT 0,
        price_image REAL NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        UNIQUE (endpoint_id, model)
    );
    ALTER TABLE endpoints ADD COLUMN available TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE endpoints ADD COLUMN available_at REAL;
    INSERT INTO models (id, endpoint_id, model, name, extra, capabilities, price_in, price_out, price_image, enabled, created_at)
        SELECT 'm_' || id, id, model, model, '{}', capabilities, price_in, price_out, price_image, enabled, created_at
        FROM endpoints WHERE kind IN ('text', 'vision', 'image') AND model <> '';
    UPDATE endpoints SET kind = 'openai', capabilities = '{}' WHERE kind IN ('text', 'vision', 'image');
    """,
]

_local = threading.local()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def connect() -> sqlite3.Connection:
    """每个线程复用一个连接（sqlite3 连接不可跨线程并发使用）。"""
    s = get_settings()
    key = str(s.db_path)
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    c = conns.get(key)
    if c is None:
        c = conns[key] = _connect(s.db_path)
    return c


def close_thread_conn() -> None:
    conns = getattr(_local, "conns", None) or {}
    for c in conns.values():
        try:
            c.close()
        except Exception:
            pass
    _local.conns = {}


@contextmanager
def tx(conn: sqlite3.Connection | None = None, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """显式事务。immediate=True 时开始即获取写锁。支持嵌套（内层直接复用外层事务）。"""
    conn = conn or connect()
    if conn.in_transaction:
        yield conn
        return
    for attempt in range(50):
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            break
        except sqlite3.OperationalError as e:  # database is locked
            if "locked" not in str(e) or attempt == 49:
                raise
            time.sleep(0.05 * (attempt + 1))
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def migrate(conn: sqlite3.Connection | None = None) -> None:
    conn = conn or connect()
    # 锁内读取版本号，避免多个 Web/worker 同时把同一迁移执行两次。
    with tx(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        cur = int(row["value"]) if row else 0
        for i, sql in enumerate(MIGRATIONS, start=1):
            if i <= cur:
                continue
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(i),),
            )


def _split_sql(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


# ---------- 便捷函数 ----------

def one(sql: str, args: Iterable[Any] = (), conn: sqlite3.Connection | None = None) -> dict | None:
    r = (conn or connect()).execute(sql, tuple(args)).fetchone()
    return dict(r) if r else None


def all_(sql: str, args: Iterable[Any] = (), conn: sqlite3.Connection | None = None) -> list[dict]:
    return [dict(r) for r in (conn or connect()).execute(sql, tuple(args)).fetchall()]


def run(sql: str, args: Iterable[Any] = (), conn: sqlite3.Connection | None = None) -> int:
    cur = (conn or connect()).execute(sql, tuple(args))
    return cur.rowcount


def insert(table: str, row: dict, conn: sqlite3.Connection | None = None) -> None:
    cols = list(row.keys())
    vals = [_enc(row[c]) for c in cols]
    q = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    (conn or connect()).execute(q, vals)


def update(table: str, key: dict, values: dict, conn: sqlite3.Connection | None = None) -> int:
    sets = ", ".join(f"{k}=?" for k in values)
    where = " AND ".join(f"{k}=?" for k in key)
    q = f"UPDATE {table} SET {sets} WHERE {where}"
    cur = (conn or connect()).execute(q, [_enc(v) for v in values.values()] + list(key.values()))
    return cur.rowcount


def _enc(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def jload(v: str | None, default: Any = None) -> Any:
    if v is None or v == "":
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def get_setting(key: str, default: Any = None) -> Any:
    r = one("SELECT value FROM meta WHERE key=?", (f"setting:{key}",))
    return jload(r["value"], default) if r else default


def set_setting(key: str, value: Any) -> None:
    run(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"setting:{key}", json.dumps(value, ensure_ascii=False)),
    )
