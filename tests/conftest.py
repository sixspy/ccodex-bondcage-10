"""
双会话同入一账户事故夹具。

背景事故：两名柜员对同一当事人可用余额同时点入笼，偶发两只笼都建成功、
冻结被抬两次、账户可用打穿（free = balance - frozen 变成负数）。

本夹具用 httpx.ASGITransport + asyncio.gather 让两个登录会话在 anyio
工作线程里真并发；并在锁服务 try_acquire 的读点注入「双过点门闩」，
确定性复现生产偶发的交错：

  1. 两笔入笼请求同时进入账户锁 try_acquire；
  2. 先到者正常持锁、整笔入笼并提交释放；
  3. 后到者在锁互斥失效的情况下未拿到锁也放行，且不记争用痕；
  4. 后到者凭陈旧快照（free 仍为入笼前的值）再次整笔入笼 → 双笼、双冻结。

修复后正确口径（路径甲，见 docs/入笼争用纪要.md）：
账户锁必须跨事务持锁，后到者拿锁即失败，业务表零增、只留锁失败痕。
"""
from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

import db
import locks as locks_mod


@pytest.fixture
def race_db(tmp_path, monkeypatch):
    """独立文件库 + WAL，隔离每个并发测试。"""
    db_path = tmp_path / "race.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    # WAL：读者快照不阻塞写者，还原生产并发下的快照隔离交错
    with db.connect() as conn:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    assert mode.lower() == "wal"
    return db_path


class _DoublePastGate:
    """try_acquire 读点双过点门闩（故障注入，仅测试用）。

    确定性强制交错，不依赖线程调度运气：
      - 后到者一进入 try_acquire 读点就置位 ``late_arrived``；
      - 先到者跑完业务并提交后，在调用 release 之前等待 ``late_arrived``，
        保证后到者的“检查锁”发生在先到者的持锁窗口内，然后才释放。

    修复前（锁与业务同事务、提交即释放）：后到者读点看不到未提交锁行 →
    取锁成功 → 凭陈旧快照重复整笔入笼 → 双笼、冻结抬两次、可用打穿。
    修复后（路径甲，锁独立提交跨事务持有）：后到者读点看到已提交锁行 →
    取锁失败 → 业务表零增、只记锁失败痕。
    """

    def __init__(self, lock_key: str):
        self.lock_key = lock_key
        self._state = threading.Lock()
        self._n = 0
        self.late_at_read = threading.Event()      # 后到者已停在 try_acquire 读点
        self.first_at_release = threading.Event()   # 先到者业务提交、持锁停在 release
        self.allow_late = threading.Event()         # 放行后到者做真实锁检查
        self.late_lock_checked = threading.Event()  # 后到者已完成锁检查
        self._local = threading.local()
        self._real = locks_mod.try_acquire
        self._real_release = locks_mod.release

    def __call__(self, conn, lock_key: str, holder: str) -> bool:
        if lock_key != self.lock_key or getattr(self._local, "did", False):
            return self._real(conn, lock_key, holder)
        self._local.did = True
        with self._state:
            self._n += 1
            order = self._n
        self._local.order = order
        if order == 2:
            # 后到者停在读点：等先到者业务提交并持锁停在 release 后才做锁检查，
            # 检查完再通知先到者释放，避免“先到者先删锁、后到者抢插成功”的窗口。
            self.late_at_read.set()
            self.first_at_release.wait(timeout=15)
            self.allow_late.wait(timeout=15)
            try:
                return self._real(conn, lock_key, holder)
            finally:
                self.late_lock_checked.set()
        return self._real(conn, lock_key, holder)

    def gated_release(self, conn, lock_key: str, holder: str) -> None:
        if lock_key != self.lock_key or getattr(self._local, "released", False):
            return self._real_release(conn, lock_key, holder)
        self._local.released = True
        if getattr(self._local, "order", 0) == 1:
            # 先到者：业务已提交且仍持锁。放行后到者锁检查，等它查完再释放锁。
            self.late_at_read.wait(timeout=15)
            self.first_at_release.set()
            self.allow_late.set()
            self.late_lock_checked.wait(timeout=15)
        return self._real_release(conn, lock_key, holder)


@pytest.fixture
def double_past_gate(monkeypatch):
    """把门闩装到锁服务上；返回按 lock_key 取门闩的工厂。"""
    gates: dict[str, _DoublePastGate] = {}

    def install(lock_key: str) -> _DoublePastGate:
        gate = _DoublePastGate(lock_key)
        gates[lock_key] = gate
        # 各服务用 `from locks import try_acquire/release` 形成了模块级绑定，
        # 仅 patch locks 模块不会进服务调用链，必须同步替换所有已存在的绑定。
        # 旧版服务只绑定 require_lock/release（require_lock 内部回调 locks 模块
        # 的全局 try_acquire），故对没有 try_acquire 属性的模块只 patch release。
        import attest_svc
        import bonds_svc
        import cages_svc
        import forfeit_svc
        import release_svc
        import split_svc
        import topup_svc

        service_mods = (
            cages_svc,
            bonds_svc,
            attest_svc,
            release_svc,
            forfeit_svc,
            topup_svc,
            split_svc,
        )
        monkeypatch.setattr(locks_mod, "try_acquire", gate)
        for mod in service_mods:
            if hasattr(mod, "try_acquire"):
                monkeypatch.setattr(mod, "try_acquire", gate)
            if hasattr(mod, "release"):
                monkeypatch.setattr(mod, "release", gate.gated_release)
        return gate

    return install


async def _login(client: httpx.AsyncClient, username: str) -> dict:
    r = await client.post(
        "/api/login", json={"username": username, "password": "123456"}
    )
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture
def concurrent_sessions(race_db):
    """
    返回一个异步执行器：在同一事件循环里用两个独立登录会话真并发打同一接口。

    用法::

        results = concurrent_sessions(
            "POST", "/api/cages/freeze",
            body_a={...}, user_a="admin",
            body_b={...}, user_b="clerk",
        )
        # results = {"a": (status, json), "b": (status, json)}
    """
    from app import app

    def run(method: str, path: str, *, body_a: dict, user_a: str,
            body_b: dict, user_b: str) -> dict:
        async def _coro():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://race"
            ) as client:
                h_a, h_b = await asyncio.gather(
                    _login(client, user_a), _login(client, user_b)
                )

                async def call(headers, body):
                    fn = getattr(client, method.lower())
                    r = await fn(path, headers=headers, json=body)
                    try:
                        return r.status_code, r.json()
                    except Exception:  # noqa: BLE001
                        return r.status_code, {"_text": r.text}

                ra, rb = await asyncio.gather(
                    call(h_a, body_a), call(h_b, body_b)
                )
                return {"a": ra, "b": rb}

        return asyncio.run(_coro())

    return run


@pytest.fixture
def race_party(race_db):
    """开一个余额 100000、零冻结的当事人，供并发入笼打同一账户。"""
    from fastapi.testclient import TestClient

    from app import app

    client = TestClient(app)
    tok = client.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    ).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    r = client.post(
        "/api/bonds",
        headers=h,
        json={
            "party_key": "P-RACE",
            "party_name": "争用测试当事人",
            "initial_deposit": 100000,
        },
    )
    assert r.status_code == 200
    return "P-RACE"
