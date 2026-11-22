"""笼锁与争用登记（公共并发面）。

路径甲（见 docs/入笼争用纪要.md）：
- 取锁（try_acquire）与放锁（release）各自独立提交，锁行跨事务持有、
  在任何业务写之前就对全库可见，杜绝“锁与业务同事务、互斥失效”；
- 拿不到锁只记锁失败痕（contention_log + 调用方审计），业务表零增；
- 偏航写法记「入笼争用偏航」并由调用方拒写。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    """尝试取锁。仅写 cage_locks / contention_log，不碰任何业务表。

    用 INSERT OR IGNORE 把“检查 + 插入”收成一条原子语句，避免两个并发
    事务都 SELECT 未见锁行后双双 INSERT 撞主键。成功时锁行立即提交、
    跨事务持有；失败时只留争用痕并提交。
    """
    now = now_iso()
    cur = conn.execute(
        "INSERT OR IGNORE INTO cage_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
        (lock_key, holder, now),
    )
    if cur.rowcount == 1:
        conn.commit()  # 锁行独立提交：先于一切业务写对全库可见
        return True
    # 未插入：锁已被持有。同持有者重入视为成功，否则记争用痕。
    row = conn.execute(
        "SELECT holder FROM cage_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is not None and row["holder"] == holder:
        return True
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={row['holder'] if row else '?'}", now),
    )
    conn.commit()  # 锁失败痕独立提交，不依赖业务事务回滚
    return False


def release(conn, lock_key: str, holder: str) -> None:
    """释放本会话持有的锁，独立提交。"""
    conn.execute(
        "DELETE FROM cage_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )
    conn.commit()


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


def holds_lock(conn, lock_key: str, holder: str) -> bool:
    """本会话是否确实持有该锁（路径甲整笔写入前的持锁兜底断言）。"""
    row = conn.execute(
        "SELECT holder FROM cage_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    return row is not None and row["holder"] == holder


def record_deviation(conn, actor: str, lock_key: str, detail: str) -> None:
    """登记入笼争用偏航，并落审计。调用方须随后拒写（业务表零增）。"""
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, actor, "入笼争用偏航", detail, now_iso()),
    )
    write_audit(
        conn,
        actor,
        "cage_freeze_deviation_rejected",
        lock_key,
        {"reason": detail},
    )
    conn.commit()


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cage_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(
            conn,
            user,
            "lock_acquire",
            body.lock_key,
            {"ok": ok},
        )
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：未能获得锁")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
        return {"ok": True}
