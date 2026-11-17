
import hashlib, secrets, sqlite3, threading
from datetime import datetime
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 并发边界：关日动作与放行动作串行占坑。
# 关日与放行都必须进同一道闸（GATE）后再判定+落库，后到者只准失败闭落库
# （记"关日争用"），禁止默成。只改提示不动这道闸，验收不过。
GATE = threading.RLock()

CLOSE_STANDARDS = ("甲闭法", "乙闭法")

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from typing import Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slices(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_no TEXT UNIQUE NOT NULL,
          biz_date TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          release_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS releases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watermarks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          consumer TEXT NOT NULL,
          biz_date TEXT NOT NULL,
          watermark TEXT NOT NULL,
          source_slice TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS day_closes(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          biz_date TEXT UNIQUE NOT NULL,
          standard TEXT NOT NULL,
          status TEXT NOT NULL,
          success_count INTEGER NOT NULL,
          voided_count INTEGER NOT NULL,
          policy_note TEXT NOT NULL,
          started_at TEXT NOT NULL,
          latched_at TEXT
        );
        CREATE TABLE IF NOT EXISTS contention_specs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          biz_date TEXT UNIQUE NOT NULL,
          standard TEXT NOT NULL,
          note TEXT NOT NULL,
          pinned_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS date_catalog(
          biz_date TEXT PRIMARY KEY,
          state TEXT NOT NULL,
          note TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deviations(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          biz_date TEXT NOT NULL,
          requested_standard TEXT NOT NULL,
          pinned_standard TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"] == 0:
            rows = [
                ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行"),
                ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行"),
                ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待检", "未放行"),
            ]
            for r in rows:
                conn.execute("INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)", r)
            conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (2,'ads_order','成功','完整切片放行','2026-11-01T18:00:00')")
            conn.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES ('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west')")

@app.on_event("startup")
def _start():
    init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username, role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/slices")
def list_slices(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM slices ORDER BY id").fetchall()
    return {"items": [dict(r) for r in rows]}

# ---------- 关日：口径、台账、旁注 ----------

def norm_standard(raw: str) -> str:
    s = (raw or "").strip()
    alias = {"甲": "甲闭法", "甲闭法": "甲闭法", "乙": "乙闭法", "乙闭法": "乙闭法"}
    if s not in alias:
        raise HTTPException(400, "未知关日口径：只认 甲闭法 / 乙闭法")
    return alias[s]

def spec_note(standard: str) -> str:
    return (f"关日争用说明：关日与放行串行占坑，后到记关日争用禁止默成；"
            f"本日口径钉为{standard}，落入本说明后当日不得更换；"
            f"关日中途不得把半截放行标成功，闩死后不得留可推水位草稿。")

def policy_note(standard: str) -> str:
    if standard == "甲闭法":
        return "策略旁注：甲闭法——关日启动后同一事务拒绝新的成功放行并回滚未提交草稿。"
    return "策略旁注：乙闭法——已持提交锁的在途放行允许跑完，关日等其结束后再闩死；未拿锁草稿作废且禁止再提交。"

def get_close(conn, biz_date: str):
    return conn.execute("SELECT * FROM day_closes WHERE biz_date=?", (biz_date,)).fetchone()

def success_count(conn, biz_date: str) -> int:
    return conn.execute("""SELECT COUNT(*) c FROM releases r JOIN slices s ON s.id=r.slice_id
                           WHERE s.biz_date=? AND r.status='成功'""", (biz_date,)).fetchone()["c"]

def void_drafts(conn, biz_date: str) -> int:
    """未提交草稿作废：闩死后不得再留可推水位的草稿。"""
    cur = conn.execute("UPDATE slices SET release_status='已作废' WHERE biz_date=? AND release_status='未放行'", (biz_date,))
    return cur.rowcount

def upsert_catalog(conn, biz_date: str, state: str, note: str):
    conn.execute("""INSERT INTO date_catalog(biz_date,state,note,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(biz_date) DO UPDATE SET state=excluded.state, note=excluded.note, updated_at=excluded.updated_at""",
                 (biz_date, state, note, now_iso()))

def guard_spec(conn, biz_date: str, standard: str):
    """偏离关日争用说明（当日已钉口径）就拒绝并登记关日口径偏了。"""
    spec = conn.execute("SELECT * FROM contention_specs WHERE biz_date=?", (biz_date,)).fetchone()
    if spec and spec["standard"] != standard:
        detail = f"关日口径偏了：{biz_date} 已钉 {spec['standard']}，拒绝改用 {standard}"
        conn.execute("INSERT INTO deviations(biz_date,requested_standard,pinned_standard,detail,created_at) VALUES(?,?,?,?,?)",
                     (biz_date, standard, spec["standard"], detail, now_iso()))
        conn.commit()
        raise HTTPException(409, detail)

def pin_spec(conn, biz_date: str, standard: str):
    if not conn.execute("SELECT 1 FROM contention_specs WHERE biz_date=?", (biz_date,)).fetchone():
        conn.execute("INSERT INTO contention_specs(biz_date,standard,note,pinned_at) VALUES(?,?,?,?)",
                     (biz_date, standard, spec_note(standard), now_iso()))

def close_precheck(conn, biz_date: str, standard: str):
    """钉口径、查台账。返回 ('already', ledger) 或 ('go', None)；关日进行中则后到记争用。"""
    guard_spec(conn, biz_date, standard)
    ledger = get_close(conn, biz_date)
    if ledger and ledger["status"] == "已闩死":
        return "already", ledger
    if ledger:
        raise HTTPException(409, f"关日争用：{biz_date} 关日进行中，后到禁止默成")
    pin_spec(conn, biz_date, standard)
    return "go", None

class CloseIn(BaseModel):
    standard: str

@app.post("/api/bizdates/{biz_date}/close")
def close_biz_date(biz_date: str, body: CloseIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    standard = norm_standard(body.standard)
    with GATE:
        with connect() as c:
            state, ledger = close_precheck(c, biz_date, standard)
            if state == "already":
                return {"ok": True, "already": True, "ledger": dict(ledger)}
            if standard == "甲闭法":
                # 甲闭法：同一事务闩死——拒绝新的成功放行并回滚未提交草稿
                succ = success_count(c, biz_date)
                voided = void_drafts(c, biz_date)
                ts = now_iso()
                c.execute("""INSERT INTO day_closes(biz_date,standard,status,success_count,voided_count,policy_note,started_at,latched_at)
                             VALUES(?,?,?,?,?,?,?,?)""",
                          (biz_date, standard, "已闩死", succ, voided, policy_note(standard), ts, ts))
                upsert_catalog(c, biz_date, "已闩死", f"目录旁注：{biz_date} 已闩死，成功 {succ} 笔，作废草稿 {voided} 笔")
                return {"ok": True, "already": False, "ledger": dict(get_close(c, biz_date))}
            # 乙闭法·第一段：先落"关日中"标记并提交，在途持锁放行允许跑完
            c.execute("""INSERT INTO day_closes(biz_date,standard,status,success_count,voided_count,policy_note,started_at,latched_at)
                         VALUES(?,?,?,?,?,?,?,NULL)""",
                      (biz_date, standard, "关日中", 0, 0, policy_note(standard), now_iso()))
            upsert_catalog(c, biz_date, "关日中", f"目录旁注：{biz_date} 关日中，在途持锁放行跑完后闩死")
    if standard == "乙闭法":
        # 让出占坑锁：已持提交锁的在途放行已跑完；后到放行撞"关日中"标记，记关日争用失败闭
        with GATE:
            with connect() as c:
                # 乙闭法·第二段：在途跑完后再闩死，未拿锁草稿作废且禁止再提交
                succ = success_count(c, biz_date)
                voided = void_drafts(c, biz_date)
                c.execute("""UPDATE day_closes SET status='已闩死', success_count=?, voided_count=?, latched_at=? WHERE biz_date=?""",
                          (succ, voided, now_iso(), biz_date))
                upsert_catalog(c, biz_date, "已闩死", f"目录旁注：{biz_date} 已闩死，成功 {succ} 笔，作废草稿 {voided} 笔")
                return {"ok": True, "already": False, "ledger": dict(get_close(c, biz_date))}

@app.get("/api/daycloses")
def list_daycloses(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM day_closes ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/deviations")
def list_deviations(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM deviations ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/bizdates")
def list_bizdates(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""
            SELECT d.biz_date,
                   COALESCE(cat.state, '开放') AS state,
                   COALESCE(cat.note, '目录旁注：业务日开放，可登记可放行') AS note,
                   spec.standard AS standard,
                   spec.note AS policy_note,
                   dc.status AS ledger_status,
                   dc.success_count AS success_count,
                   dc.voided_count AS voided_count
            FROM (SELECT biz_date FROM slices
                  UNION SELECT biz_date FROM day_closes
                  UNION SELECT biz_date FROM date_catalog) d
            LEFT JOIN date_catalog cat ON cat.biz_date=d.biz_date
            LEFT JOIN contention_specs spec ON spec.biz_date=d.biz_date
            LEFT JOIN day_closes dc ON dc.biz_date=d.biz_date
            ORDER BY d.biz_date""").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/bizdates/{biz_date}/reconcile")
def reconcile(biz_date: str, authorization: str | None = Header(default=None)):
    """关日台账、成功流水、切片详情、策略旁注、目录旁注五项勾稽。"""
    require_user(authorization)
    with connect() as c:
        ledger = get_close(c, biz_date)
        spec = c.execute("SELECT * FROM contention_specs WHERE biz_date=?", (biz_date,)).fetchone()
        catalog = c.execute("SELECT * FROM date_catalog WHERE biz_date=?", (biz_date,)).fetchone()
        flow = c.execute("""SELECT r.*, s.slice_no FROM releases r JOIN slices s ON s.id=r.slice_id
                            WHERE s.biz_date=? AND r.status='成功' ORDER BY r.id""", (biz_date,)).fetchall()
        contended = c.execute("""SELECT COUNT(*) c FROM releases r JOIN slices s ON s.id=r.slice_id
                                 WHERE s.biz_date=? AND r.status='拒绝' AND r.note LIKE '关日争用%'""", (biz_date,)).fetchone()["c"]
        by_status = {r["st"]: r["c"] for r in c.execute(
            "SELECT release_status st, COUNT(*) c FROM slices WHERE biz_date=? GROUP BY release_status", (biz_date,)).fetchall()}
        wm = c.execute("SELECT COUNT(*) c FROM watermarks WHERE biz_date=?", (biz_date,)).fetchone()["c"]
        devs = c.execute("SELECT * FROM deviations WHERE biz_date=? ORDER BY id", (biz_date,)).fetchall()
    checks = []
    if ledger and ledger["status"] == "已闩死":
        checks.append({"name": "台账成功数对得上成功流水", "ok": ledger["success_count"] == len(flow),
                       "detail": f"台账 {ledger['success_count']} 笔 / 流水 {len(flow)} 笔"})
        checks.append({"name": "台账作废数对得上已作废切片", "ok": ledger["voided_count"] == by_status.get("已作废", 0),
                       "detail": f"台账 {ledger['voided_count']} 笔 / 切片 {by_status.get('已作废', 0)} 张"})
        checks.append({"name": "闩死后不留可推水位草稿", "ok": by_status.get("未放行", 0) == 0,
                       "detail": f"未放行 {by_status.get('未放行', 0)} 张"})
        checks.append({"name": "目录旁注状态对得上台账", "ok": bool(catalog) and catalog["state"] == "已闩死",
                       "detail": f"目录 {catalog['state'] if catalog else '无'} / 台账 已闩死"})
        checks.append({"name": "策略旁注口径对得上台账", "ok": bool(spec) and spec["standard"] == ledger["standard"],
                       "detail": f"策略 {spec['standard'] if spec else '无'} / 台账 {ledger['standard']}"})
    elif ledger:
        checks.append({"name": "关日未闩：等在途持锁放行跑完", "ok": bool(catalog) and catalog["state"] == "关日中",
                       "detail": f"目录 {catalog['state'] if catalog else '无'} / 台账 关日中"})
    else:
        checks.append({"name": "业务日开放：无台账则无口径钉死", "ok": spec is None and catalog is None,
                       "detail": f"策略 {'有' if spec else '无'} / 目录 {'有' if catalog else '无'}"})
    return {"biz_date": biz_date,
            "ledger": dict(ledger) if ledger else None,
            "spec": dict(spec) if spec else None,
            "catalog": dict(catalog) if catalog else None,
            "success_flow": {"count": len(flow), "items": [dict(r) for r in flow]},
            "contended_count": contended,
            "slices": {"total": sum(by_status.values()), "by_status": by_status},
            "watermark_count": wm,
            "deviations": [dict(d) for d in devs],
            "checks": checks,
            "consistent": all(ch["ok"] for ch in checks)}

# ---------- 切片与放行 ----------

class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int

@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    comp = "完整" if body.row_count >= body.expected_count and body.expected_count > 0 else ("部分完整" if body.row_count > 0 else "待检")
    with GATE:
        with connect() as c:
            ledger = get_close(c, body.biz_date)
            if ledger:
                raise HTTPException(409, f"关日争用：{body.biz_date} {ledger['status']}，禁止新登记")
            try:
                cur = c.execute(
                    "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)",
                    (body.slice_no, body.biz_date, body.partition_key, body.row_count, body.expected_count, comp, "未放行"),
                )
                return {"id": cur.lastrowid, "completeness": comp}
            except sqlite3.IntegrityError:
                raise HTTPException(409, "切片号已存在")

@app.post("/api/slices/{sid}/evaluate")
def evaluate(sid: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with GATE:
        with connect() as c:
            row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
            if not row:
                raise HTTPException(404, "切片不存在")
            comp = "完整" if row["row_count"] >= row["expected_count"] and row["expected_count"] > 0 else ("部分完整" if row["row_count"] > 0 else "待检")
            c.execute("UPDATE slices SET completeness=? WHERE id=?", (comp, sid))
    return {"id": sid, "completeness": comp}

class ReleaseIn(BaseModel):
    consumer: str

@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with GATE:
        with connect() as c:
            row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
            if not row:
                raise HTTPException(404, "切片不存在")
            ledger = get_close(c, row["biz_date"])
            if ledger:
                # 关日抢写失败闭：关日启动后撞进来的放行，同事务记拒绝落库，禁止默成
                note = f"关日争用：{row['biz_date']} {ledger['status']}，后到禁止默成"
                c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                          (sid, body.consumer, "拒绝", note, now_iso()))
                c.commit()
                raise HTTPException(409, note)
            if row["release_status"] == "已作废":
                note = "关日争用：草稿已作废，禁止再提交"
                c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                          (sid, body.consumer, "拒绝", note, now_iso()))
                c.commit()
                raise HTTPException(409, note)
            if row["completeness"] != "完整":
                c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                          (sid, body.consumer, "拒绝", "完整度未达标", now_iso()))
                c.commit()
                raise HTTPException(409, "未完整禁止放行")
            c.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))
            c.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                      (sid, body.consumer, "成功", "完整切片放行", now_iso()))
            c.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES (?,?,?,?)",
                      (body.consumer, row["biz_date"], now_iso(), row["slice_no"]))
    return {"ok": True}

@app.get("/api/releases")
def list_releases(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT r.*, s.slice_no FROM releases r JOIN slices s ON s.id=r.slice_id ORDER BY r.id DESC""").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")

app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
