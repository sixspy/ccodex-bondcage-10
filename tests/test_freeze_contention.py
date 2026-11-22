"""
同账户并发入笼事故测（红 → 绿）。

事故：双会话同入一账户，偶发双笼建成、冻结抬两次、可用打穿。
口径：路径甲——持账户锁后同一提交整笔入笼；拿不到锁则业务表零增、只记锁失败痕。

事故测（红 → 绿）：
  1. test_double_session_double_cage_leak_is_blocked：双笼泄漏失败测；
  2. test_losing_session_only_records_lock_failure_trace：锁失败只记痕失败测；
  3. test_lock_released_after_success_and_business_failure：锁不泄漏；
  4. test_deviation_without_lock_is_logged_and_rejected：偏航留痕拒写。
前两条在旧缺陷代码上确定性见红，路径甲修复后全绿。
"""
from __future__ import annotations

import pytest

from db import connect

PARTY = "P-RACE"
LOCK_KEY = f"bond:{PARTY}"


def _freeze_body(who: str, code: str) -> dict:
    return {
        "party_key": PARTY,
        "contract_no": f"HT-RACE-{who}",
        "amount": 60000,
        "beneficiary": "争用受益方",
        "cage_code": code,
    }


def _account(conn):
    return conn.execute(
        "SELECT * FROM bond_accounts WHERE party_key=?", (PARTY,)
    ).fetchone()


def _cage_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM cages WHERE party_key=?", (PARTY,)
    ).fetchone()["c"]


def _contention_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM contention_log WHERE lock_key=?", (LOCK_KEY,)
    ).fetchone()["c"]


def test_double_session_double_cage_leak_is_blocked(
    race_party, double_past_gate, concurrent_sessions
):
    """双会话同入一账户：只许一只笼建成，冻结只抬一次，可用不得打穿，
    且后到者必须在持锁窗口内被跨事务锁挡下并留下锁失败痕。

    旧码（锁与业务同事务、提交即释放）在该强制交错下后到者读不到已随
    业务提交而删除的锁 → 不会产生争用痕（本断言见红），并在生产的幽灵锁
    + 陈旧快照交错下双笼打穿（见 docs/入笼争用纪要.md 与事故复现脚本）。
    路径甲把锁独立提交、跨事务持有，后到者必撞持锁窗口、留痕后失败。
    """
    double_past_gate(LOCK_KEY)  # 装上双过点门闩，确定性制造并发交错

    results = concurrent_sessions(
        "POST",
        "/api/cages/freeze",
        body_a=_freeze_body("admin", "CG-RACE-001"),
        user_a="admin",
        body_b=_freeze_body("clerk", "CG-RACE-002"),
        user_b="clerk",
    )

    statuses = sorted((results["a"][0], results["b"][0]))
    with connect() as conn:
        cage_n = _cage_count(conn)
        acct = _account(conn)
        free = float(acct["balance"]) - float(acct["frozen"])
        contention_n = _contention_count(conn)

    # 恰好一成一败
    assert statuses == [200, 409], f"并发入笼应一成一败，实际={results}"
    # 正式笼表零增到只剩一只
    assert cage_n == 1, f"双笼泄漏：建成 {cage_n} 只笼"
    # 后到者必须在持锁窗口内被挡下并可见锁失败痕（路径甲的跨事务锁判别点）
    assert contention_n >= 1, "后到者撞锁必须留争用痕，旧同事务锁在此交错下零痕"
    # 冻结只抬一次（60000），可用不得为负（打穿）
    assert float(acct["frozen"]) == pytest.approx(60000.0)
    assert free >= 0, f"可用余额被打穿：free={free}"
    assert free == pytest.approx(40000.0)


def test_losing_session_only_records_lock_failure_trace(
    race_party, double_past_gate, concurrent_sessions
):
    """后到者拿不到账户锁：业务表零增，只留锁失败痕（争用 + 审计）。"""
    double_past_gate(LOCK_KEY)

    results = concurrent_sessions(
        "POST",
        "/api/cages/freeze",
        body_a=_freeze_body("admin", "CG-RACE-001"),
        user_a="admin",
        body_b=_freeze_body("clerk", "CG-RACE-002"),
        user_b="clerk",
    )

    # 后到者必须可见锁失败（409）
    loser = results["a"] if results["a"][0] == 409 else results["b"]
    winner = results["b"] if results["a"][0] == 409 else results["a"]
    assert loser[0] == 409
    assert "争用" in str(loser[1])
    loser_code = (
        "CG-RACE-001" if results["a"][0] == 409 else "CG-RACE-002"
    )
    winner_code = "CG-RACE-002" if loser_code == "CG-RACE-001" else "CG-RACE-001"

    with connect() as conn:
        # 业务表零增：仍只有一只笼，败者的笼编码不得落表
        assert _cage_count(conn) == 1
        assert conn.execute(
            "SELECT id FROM cages WHERE cage_code=?", (loser_code,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT id FROM cages WHERE cage_code=?", (winner_code,)
        ).fetchone() is not None
        # 锁失败痕可见
        assert _contention_count(conn) >= 1, "后到者锁失败必须留争用痕"
        trace = conn.execute(
            "SELECT * FROM contention_log WHERE lock_key=? ORDER BY id DESC LIMIT 1",
            (LOCK_KEY,),
        ).fetchone()
        assert trace["actor"] in ("admin", "clerk")
        # 审计页也应能看到锁失败事件
        audit = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='cage_freeze_lock_failed'"
        ).fetchone()["c"]
        assert audit >= 1, "锁失败须落审计，保证审计页可见"
        # 账户冻结只抬一次
        acct = _account(conn)
        assert float(acct["frozen"]) == pytest.approx(60000.0)


def test_lock_released_after_success_and_business_failure(
    race_party, concurrent_sessions
):
    """路径甲：成功或业务校验失败后，账户锁都必须释放，不残留、不阻塞后续。"""
    from fastapi.testclient import TestClient

    from app import app

    c = TestClient(app)
    tok = c.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    ).json()["token"]
    h = {"Authorization": "Bearer " + tok}

    def held_locks():
        with connect() as conn:
            return conn.execute(
                "SELECT holder FROM cage_locks WHERE lock_key=?", (LOCK_KEY,)
            ).fetchall()

    # 正常入笼成功后锁应释放
    r = c.post(
        "/api/cages/freeze",
        headers=h,
        json=_freeze_body("admin", "CG-REL-001"),
    )
    assert r.status_code == 200
    assert held_locks() == []

    # 业务校验失败（可用不足）后锁也应释放，且后续入笼不被残留锁挡住
    r_bad = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": PARTY,
            "contract_no": "HT-BAD",
            "amount": 999999,
            "beneficiary": "受益方",
            "cage_code": "CG-BAD-001",
        },
    )
    assert r_bad.status_code == 409
    assert held_locks() == []

    # 仍可用剩余额度再建一只笼，证明锁未泄漏
    r2 = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": PARTY,
            "contract_no": "HT-REL-2",
            "amount": 10000,
            "beneficiary": "受益方",
            "cage_code": "CG-REL-002",
        },
    )
    assert r2.status_code == 200
    assert held_locks() == []
    with connect() as conn:
        assert _cage_count(conn) == 2


def test_deviation_without_lock_is_logged_and_rejected(
    race_party, monkeypatch
):
    """偏航兜底：若取锁被旁路（未持锁即整笔写入），记偏航并业务表零增。"""
    import cages_svc

    # 模拟偏航写法：try_acquire 被旁路为“未持锁也放行”
    monkeypatch.setattr(cages_svc, "try_acquire", lambda *a, **k: True)

    from fastapi.testclient import TestClient

    from app import app

    c = TestClient(app)
    tok = c.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    ).json()["token"]
    h = {"Authorization": "Bearer " + tok}

    r = c.post(
        "/api/cages/freeze",
        headers=h,
        json=_freeze_body("admin", "CG-DEV-001"),
    )
    assert r.status_code == 409
    assert "偏航" in r.json()["detail"]

    with connect() as conn:
        # 业务表零增：偏航笼不得落表、冻结不得抬
        assert _cage_count(conn) == 0
        acct = _account(conn)
        assert float(acct["frozen"]) == pytest.approx(0.0)
        # 偏航痕与拒写审计可见
        dev = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE action='入笼争用偏航'"
        ).fetchone()["c"]
        assert dev >= 1
        audit = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail "
            "WHERE action='cage_freeze_deviation_rejected'"
        ).fetchone()["c"]
        assert audit >= 1
        # 未真正持锁，cage_locks 不应残留
        leak = conn.execute(
            "SELECT COUNT(*) c FROM cage_locks WHERE lock_key=?", (LOCK_KEY,)
        ).fetchone()["c"]
        assert leak == 0
