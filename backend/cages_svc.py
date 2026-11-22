"""入笼冻结：可用保证金划入笼内，写笼代。

路径甲（docs/入笼争用纪要.md）：账户锁跨事务持锁，锁内同一提交整笔入笼
（插笼 + 抬冻结 + 审计）；拿不到锁则业务表零增，只记锁失败痕。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import holds_lock, record_deviation, release, try_acquire
from models import CageFreezeIn

router = APIRouter(tags=["cages"])


def _next_cage_code(conn, party_key: str) -> str:
    n = conn.execute(
        "SELECT COUNT(*) c FROM cages WHERE party_key=?", (party_key,)
    ).fetchone()["c"]
    return f"CG-{party_key}-{n + 1:03d}"


@router.get("/api/cages")
def list_cages(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM cages WHERE status=? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM cages ORDER BY id DESC").fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/cages/{cage_id}")
def get_cage(cage_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        cage = conn.execute("SELECT * FROM cages WHERE id=?", (cage_id,)).fetchone()
        if not cage:
            raise HTTPException(404, "笼不存在")
        atts = conn.execute(
            "SELECT * FROM attestations WHERE cage_id=? ORDER BY id DESC",
            (cage_id,),
        ).fetchall()
        rels = conn.execute(
            "SELECT * FROM releases WHERE cage_id=? ORDER BY id DESC",
            (cage_id,),
        ).fetchall()
        fors = conn.execute(
            "SELECT * FROM forfeits WHERE cage_id=? ORDER BY id DESC",
            (cage_id,),
        ).fetchall()
        return {
            "cage": dict(cage),
            "attestations": [dict(a) for a in atts],
            "releases": [dict(r) for r in rels],
            "forfeits": [dict(f) for f in fors],
        }


@router.post("/api/cages/freeze")
def freeze_cage(body: CageFreezeIn, authorization: AuthHeader = None):
    """从账户可用余额划入新笼并冻结（路径甲：账户锁内整笔入笼）。"""
    user = require_writer(authorization)
    lock_key = f"bond:{body.party_key}"
    # 第一步：取账户锁，独立提交。拿不到锁 → 业务表零增，只记锁失败痕。
    with connect() as conn:
        if not try_acquire(conn, lock_key, user):
            # 争用痕已由 try_acquire 写入；这里补审计，随后直接拒写。
            write_audit(
                conn,
                user,
                "cage_freeze_lock_failed",
                lock_key,
                {
                    "party_key": body.party_key,
                    "contract_no": body.contract_no,
                    "amount": float(body.amount),
                    "reason": "account_lock_busy",
                },
            )
            conn.commit()
            raise HTTPException(409, "争用：账户锁被占用，后到只记锁失败痕，不写业务")

    # 第二步：持锁内同一提交整笔入笼。任何失败都回滚业务、再释放锁。
    try:
        with connect() as conn:
            # 偏航兜底：整笔写入前必须确实持有账户锁，否则记偏航并拒写。
            if not holds_lock(conn, lock_key, user):
                record_deviation(
                    conn, user, lock_key, "入笼未持账户锁即整笔写入，拒写"
                )
                raise HTTPException(
                    409, "入笼争用偏航：未持有账户锁，业务表零增拒写"
                )
            acct = conn.execute(
                "SELECT * FROM bond_accounts WHERE party_key=?",
                (body.party_key,),
            ).fetchone()
            if not acct:
                raise HTTPException(404, "保证金户不存在")
            if acct["status"] != "正常":
                raise HTTPException(409, "账户状态禁止入笼")
            amount = float(body.amount)
            free = float(acct["balance"]) - float(acct["frozen"])
            if amount > free + 1e-9:
                raise HTTPException(409, "可用保证金不足，无法入笼")
            code = body.cage_code or _next_cage_code(conn, body.party_key)
            dup = conn.execute(
                "SELECT id FROM cages WHERE cage_code=?", (code,)
            ).fetchone()
            if dup:
                raise HTTPException(409, "笼编码已存在")
            now = now_iso()
            cur = conn.execute(
                """INSERT INTO cages(cage_code,party_key,contract_no,amount,available,
                   generation,status,beneficiary,created_at,frozen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    code,
                    body.party_key,
                    body.contract_no,
                    amount,
                    amount,
                    1,
                    "已冻结",
                    body.beneficiary,
                    now,
                    now,
                ),
            )
            cage_id = cur.lastrowid
            # 扣可用即抬冻结；与插笼、审计同一提交，整笔原子
            conn.execute(
                "UPDATE bond_accounts SET frozen = frozen + ? WHERE party_key=?",
                (amount, body.party_key),
            )
            write_audit(
                conn,
                user,
                "cage_freeze",
                code,
                {
                    "cage_id": cage_id,
                    "amount": amount,
                    "contract_no": body.contract_no,
                    "beneficiary": body.beneficiary,
                    "generation": 1,
                    "lock_key": lock_key,
                },
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM cages WHERE id=?", (cage_id,)
            ).fetchone()
            return dict(row)
    finally:
        # 第三步：业务提交后独立提交释放锁。
        with connect() as conn:
            release(conn, lock_key, user)
