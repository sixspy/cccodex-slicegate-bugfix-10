
import threading

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _login(c):
    r = c.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _mk_slice(c, h, no, date, rows=10, exp=10):
    r = c.post("/api/slices", headers=h, json={
        "slice_no": no, "biz_date": date, "partition_key": "mid",
        "row_count": rows, "expected_count": exp})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _close(c, h, date, standard):
    return c.post(f"/api/bizdates/{date}/close", headers=h, json={"standard": standard})


def _date_rows(c, h, date, slice_nos):
    items = c.get("/api/releases", headers=h).json()["items"]
    return [r for r in items if r["slice_no"] in slice_nos]


def test_yi_close_blocks_release_and_voids_drafts():
    c = TestClient(app)
    h = _login(c)
    sid = _mk_slice(c, h, "SL-C-1", "2026-12-01")

    r = _close(c, h, "2026-12-01", "乙闭法")
    assert r.status_code == 200, r.text
    ledger = r.json()["ledger"]
    assert ledger["status"] == "已闩死"
    assert ledger["standard"] == "乙闭法"
    assert ledger["success_count"] == 0
    assert ledger["voided_count"] == 1  # 未拿锁草稿作废

    # 闩死后放行：失败闭落库，记关日争用，禁止默成
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    assert rel.status_code == 409
    assert "关日争用" in rel.json()["detail"]
    rows = _date_rows(c, h, "2026-12-01", {"SL-C-1"})
    assert len(rows) == 1 and rows[0]["status"] == "拒绝" and "关日争用" in rows[0]["note"]

    # 草稿已作废，不留可推水位草稿
    s = [x for x in c.get("/api/slices", headers=h).json()["items"] if x["slice_no"] == "SL-C-1"][0]
    assert s["release_status"] == "已作废"
    assert not any(w["source_slice"] == "SL-C-1" for w in c.get("/api/watermarks", headers=h).json()["items"])

    # 五账勾稽一致
    rec = c.get("/api/bizdates/2026-12-01/reconcile", headers=h).json()
    assert rec["consistent"] is True, rec
    assert rec["ledger"]["success_count"] == rec["success_flow"]["count"]


def test_jia_close_counts_success_and_pins_standard():
    c = TestClient(app)
    h = _login(c)
    sid1 = _mk_slice(c, h, "SL-C-2", "2026-12-02")
    _mk_slice(c, h, "SL-C-3", "2026-12-02")
    ok = c.post(f"/api/slices/{sid1}/release", headers=h, json={"consumer": "ads_a"})
    assert ok.status_code == 200  # 关日前已成功一笔

    r = _close(c, h, "2026-12-02", "甲闭法")
    assert r.status_code == 200, r.text
    ledger = r.json()["ledger"]
    assert ledger["success_count"] == 1   # 台账与成功流水同源
    assert ledger["voided_count"] == 1    # 未提交草稿同一事务回滚
    assert ledger["started_at"] == ledger["latched_at"]  # 甲闭法单事务即闩

    # 同口径重关：幂等，不重复落账
    again = _close(c, h, "2026-12-02", "甲闭法")
    assert again.status_code == 200 and again.json()["already"] is True
    assert len([d for d in c.get("/api/daycloses", headers=h).json()["items"] if d["biz_date"] == "2026-12-02"]) == 1

    # 换口径：偏离关日争用说明，拒绝并登记关日口径偏了
    dev = _close(c, h, "2026-12-02", "乙闭法")
    assert dev.status_code == 409 and "关日口径偏了" in dev.json()["detail"]
    devs = c.get("/api/deviations", headers=h).json()["items"]
    assert any(d["biz_date"] == "2026-12-02" and d["pinned_standard"] == "甲闭法" and d["requested_standard"] == "乙闭法" for d in devs)

    rec = c.get("/api/bizdates/2026-12-02/reconcile", headers=h).json()
    assert rec["consistent"] is True, rec


def test_release_during_closing_is_rejected_fail_closed():
    c = TestClient(app)
    h = _login(c)
    sid = _mk_slice(c, h, "SL-C-4", "2026-12-03")
    # 白盒：乙闭法第一段已落"关日中"标记，第二段尚未闩死
    with app_module.connect() as conn:
        conn.execute("INSERT INTO day_closes(biz_date,standard,status,success_count,voided_count,policy_note,started_at,latched_at) VALUES (?,?,?,?,?,?,?,NULL)",
                     ("2026-12-03", "乙闭法", "关日中", 0, 0, "策略旁注：乙闭法", "2026-12-03T23:59:00"))
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    assert rel.status_code == 409 and "关日争用" in rel.json()["detail"]
    rows = _date_rows(c, h, "2026-12-03", {"SL-C-4"})
    assert len(rows) == 1 and rows[0]["status"] == "拒绝" and "关日争用" in rows[0]["note"]
    # 关日中途不得把半截放行标成功
    s = [x for x in c.get("/api/slices", headers=h).json()["items"] if x["slice_no"] == "SL-C-4"][0]
    assert s["release_status"] == "未放行"
    assert not any(w["source_slice"] == "SL-C-4" for w in c.get("/api/watermarks", headers=h).json()["items"])


def test_register_on_closed_date_rejected():
    c = TestClient(app)
    h = _login(c)
    assert _close(c, h, "2026-12-05", "甲闭法").status_code == 200
    r = c.post("/api/slices", headers=h, json={
        "slice_no": "SL-C-5", "biz_date": "2026-12-05", "partition_key": "mid",
        "row_count": 10, "expected_count": 10})
    assert r.status_code == 409 and "关日争用" in r.json()["detail"]


def test_close_release_race_serializes_fail_closed():
    c = TestClient(app)
    h = _login(c)
    date = "2026-12-04"
    nos = [f"SL-R-{i}" for i in range(6)]
    sids = [_mk_slice(c, h, no, date) for no in nos]

    barrier = threading.Barrier(7)
    results = {"release": [], "close": []}

    def do_release(sid):
        cc = TestClient(app)
        barrier.wait(timeout=10)
        r = cc.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_race"})
        results["release"].append(r.status_code)

    def do_close():
        cc = TestClient(app)
        barrier.wait(timeout=10)
        r = cc.post(f"/api/bizdates/{date}/close", headers=h, json={"standard": "乙闭法"})
        results["close"].append(r.status_code)

    threads = [threading.Thread(target=do_release, args=(s,)) for s in sids]
    threads.append(threading.Thread(target=do_close))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)

    assert results["close"] == [200]
    n_ok = results["release"].count(200)
    n_rej = results["release"].count(409)
    assert n_ok + n_rej == 6

    rows = _date_rows(c, h, date, set(nos))
    succ = [r for r in rows if r["status"] == "成功"]
    rej = [r for r in rows if r["status"] == "拒绝" and "关日争用" in r["note"]]
    # 每次放行落且仅落一条流水；成功者必有水位，被撞者失败闭落库
    assert len(succ) == n_ok and len(rej) == n_rej
    wm = [w for w in c.get("/api/watermarks", headers=h).json()["items"] if w["biz_date"] == date]
    assert len(wm) == n_ok

    # 台账与成功流水同数；闩死后无未放行草稿
    ledger = [d for d in c.get("/api/daycloses", headers=h).json()["items"] if d["biz_date"] == date][0]
    assert ledger["status"] == "已闩死"
    assert ledger["success_count"] == len(succ)
    left = [s for s in c.get("/api/slices", headers=h).json()["items"] if s["biz_date"] == date and s["release_status"] == "未放行"]
    assert left == []

    rec = c.get(f"/api/bizdates/{date}/reconcile", headers=h).json()
    assert rec["consistent"] is True, rec
