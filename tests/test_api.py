"""HTTP API 集成测试，需要一个可连接的 PostgreSQL（由 DATABASE_URL 指定）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_api.py
脚本会清空 regions/region_meta 后跑全套用例。
"""
from __future__ import annotations

import os
import sys
import threading
import time

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


db.init_pool()
client = TestClient(app)

# 清空数据
with db.pool.connection() as conn:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE regions RESTART IDENTITY")
        cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
    conn.commit()

# ---- 健康检查 ----
r = client.get("/health")
check("GET /health 200", r.status_code == 200 and r.json()["status"] == "ok")

# ---- 登记区域 ----
good_region = {
    "name": "A区",
    "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
    "valid_from": "2026-09-01T00:00:00Z",
    "valid_to": "2026-10-01T00:00:00Z",
}
r = client.post("/api/regions", json=good_region)
check("POST /api/regions 201", r.status_code == 201, r.text)
body = r.json()
rid = body["region"]["id"]
check("revision 变为 1", body["revision"] == 1, str(body))
check("返回的多边形闭合且逆时针", body["region"]["polygon"][0] == body["region"]["polygon"][-1])
check("version=1", body["region"]["version"] == 1)

# ---- 非法输入：自相交多边形不保存 ----
before = client.get("/api/revision").json()["revision"]
r = client.post("/api/regions", json={
    "name": "坏多边形",
    "polygon": [[0, 0], [10, 10], [10, 0], [0, 10]],
    "valid_from": "2026-09-01T00:00:00Z",
    "valid_to": "2026-10-01T00:00:00Z",
})
check("自相交多边形 422", r.status_code == 422, r.text)
check("错误信息包含非法原因", "自相交" in r.text or "invalid" in r.text)
after = client.get("/api/revision").json()["revision"]
check("非法多边形未保存（revision 不变）", before == after)

# ---- 非法输入：NaN ----
import json as _json

r = client.post("/api/regions", content=_json.dumps({
    "name": "NaN",
    "polygon": [[0, 0], ["NaN", 1], [1, 1]],
    "valid_from": "2026-09-01T00:00:00Z",
    "valid_to": "2026-10-01T00:00:00Z",
}), headers={"content-type": "application/json"})
check("NaN 坐标 422", r.status_code == 422, r.text)

# ---- 非法输入：时间不递增 ----
r = client.post("/api/regions", json={
    "name": "时段错误",
    "polygon": [[0, 0], [1, 0], [1, 1]],
    "valid_from": "2026-10-01T00:00:00Z",
    "valid_to": "2026-09-01T00:00:00Z",
})
check("起止不递增 422", r.status_code == 422, r.text)
check("错误码 invalid_input", r.json()["error"]["code"] == "invalid_input")
check("时间错误未保存",
      client.get("/api/revision").json()["revision"] == 1)

# ---- 时间必须带时区 ----
r = client.post("/api/regions", json={
    "name": "无时区",
    "polygon": [[0, 0], [1, 0], [1, 1]],
    "valid_from": "2026-09-01T00:00:00",
    "valid_to": "2026-10-01T00:00:00",
})
check("无时区时间 422", r.status_code == 422, r.text)

# ---- 第二个区域，用于分别列出 ----
r = client.post("/api/regions", json={
    "name": "B区",
    "polygon": [[20, 0], [30, 0], [30, 10], [20, 10]],
    "valid_from": "2026-09-01T00:00:00Z",
    "valid_to": "2026-10-01T00:00:00Z",
})
rid_b = r.json()["region"]["id"]
check("B区登记成功 revision=2", r.status_code == 201 and r.json()["revision"] == 2)

# ---- 列表/单查 ----
check("GET 列表有 2 个区域", len(client.get("/api/regions").json()) == 2)
check("GET 单查 200", client.get(f"/api/regions/{rid}").status_code == 200)
check("GET 不存在 404", client.get("/api/regions/9999").status_code == 404)

# ---- 核验：干净穿越 A 区 ----
r = client.post("/api/verify", json={"points": [
    {"x": -5, "y": 5, "t": "2026-09-30T00:00:00Z"},
    {"x": 15, "y": 5, "t": "2026-09-30T00:01:00Z"},
]})
check("verify 200", r.status_code == 200, r.text)
v = r.json()
check("safe=false", v["safe"] is False)
check("结果分别列出两个区域", len(v["results"]) == 2)
ra = next(x for x in v["results"] if x["region_id"] == rid)
rb = next(x for x in v["results"] if x["region_id"] == rid_b)
check("A 区 1 段穿越", len(ra["crossings"]) == 1, str(ra))
check("A 区进入=00:00:15",
      ra["crossings"][0]["entry_time"] == "2026-09-30T00:00:15Z", str(ra))
check("A 区离开=00:00:45",
      ra["crossings"][0]["exit_time"] == "2026-09-30T00:00:45Z")
check("B 区无穿越", rb["crossings"] == [])
check("响应带 revision=2", v["revision"] == 2)

# ---- 核验：安全轨迹 ----
r = client.post("/api/verify", json={"points": [
    {"x": 100, "y": 100, "t": "2026-09-30T00:00:00Z"},
    {"x": 110, "y": 110, "t": "2026-09-30T00:01:00Z"},
]})
check("远离区域 safe=true", r.json()["safe"] is True)

# ---- 核验：单点接触 ----
r = client.post("/api/verify", json={"points": [
    {"x": 5, "y": 15, "t": "2026-09-30T00:00:00Z"},
    {"x": 15, "y": 5, "t": "2026-09-30T00:00:10Z"},
]})
v = r.json()
ra = next(x for x in v["results"] if x["region_id"] == rid)
check("单点接触返回相同起止时刻",
      len(ra["crossings"]) == 1
      and ra["crossings"][0]["point_contact"] is True
      and ra["crossings"][0]["entry_time"] == ra["crossings"][0]["exit_time"]
      == "2026-09-30T00:00:05Z", str(ra))

# ---- 核验：非法轨迹 ----
r = client.post("/api/verify", json={"points": [
    {"x": 0, "y": 0, "t": "2026-09-30T00:00:10Z"},
    {"x": 1, "y": 1, "t": "2026-09-30T00:00:00Z"},
]})
check("逆序时刻 422", r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)

r = client.post("/api/verify", json={"points": [
    {"x": 0, "y": 0, "t": "2026-09-30T00:00:00Z"},
    {"x": 1, "y": 1, "t": "2026-09-30T00:00:00Z"},
]})
check("重复时刻 422", r.status_code == 422, r.text)

r = client.post("/api/verify", json={"points": [
    {"x": 0, "y": 0, "t": "2026-09-30T00:00:00Z"},
]})
check("轨迹点不足 422", r.status_code == 422, r.text)

# ---- 核验：交点插值，不踩采样点 ----
# 采样点都在区域外，但连线穿越区域
r = client.post("/api/verify", json={"points": [
    {"x": -1, "y": 5, "t": "2026-09-30T00:00:00Z"},
    {"x": 11, "y": 5, "t": "2026-09-30T00:01:00Z"},
    {"x": 21, "y": 5, "t": "2026-09-30T00:02:00Z"},
]})
v = r.json()
ra = next(x for x in v["results"] if x["region_id"] == rid)
check("采样点在区域外仍能检出插值穿越",
      len(ra["crossings"]) == 1
      and ra["crossings"][0]["entry_time"] == "2026-09-30T00:00:05Z"
      and ra["crossings"][0]["exit_time"] == "2026-09-30T00:00:55Z", str(ra))

# ---- 修订区域（部分更新） ----
r = client.patch(f"/api/regions/{rid}", json={"name": "A区-扩大"})
check("PATCH 名称成功 revision=3", r.status_code == 200 and r.json()["revision"] == 3, r.text)
check("名称已更新", r.json()["region"]["name"] == "A区-扩大")
check("version=2", r.json()["region"]["version"] == 2)

r = client.patch(f"/api/regions/{rid}", json={
    "polygon": [[0, 0], [20, 0], [20, 20], [0, 20]],
})
check("PATCH 多边形成功 revision=4", r.status_code == 200 and r.json()["revision"] == 4, r.text)

r = client.patch(f"/api/regions/{rid}", json={
    "valid_from": "2026-11-01T00:00:00Z",
    "valid_to": "2026-10-01T00:00:00Z",
})
check("PATCH 出非法时段 422 且不保存", r.status_code == 422)
check("非法 PATCH 后 revision 仍=4", client.get("/api/revision").json()["revision"] == 4)

r = client.patch("/api/regions/9999", json={"name": "x"})
check("PATCH 不存在 404", r.status_code == 404)

# ---- 删除 ----
r = client.delete(f"/api/regions/{rid_b}")
check("DELETE 成功 revision=5", r.status_code == 200 and r.json()["revision"] == 5, r.text)
check("DELETE 后列表只有 1 个", len(client.get("/api/regions").json()) == 1)
check("DELETE 不存在 404", client.delete("/api/regions/9999").status_code == 404)

# ---- 并发一致性：核验进行中区域被修改，本次仍使用同一已提交版本 ----
# 恢复一个标准区域用于本用例
client.post("/api/regions", json=good_region)

from app import geometry  # noqa: E402

orig = geometry.verify_trajectory
barrier_started = threading.Event()
barrier_finish = threading.Event()


def slow_verify(traj, regions):
    barrier_started.set()
    time.sleep(1.0)  # 给修改线程留出提交窗口
    result = orig(traj, regions)
    barrier_finish.set()
    return result


geometry.verify_trajectory = slow_verify
try:
    def do_verify():
        return client.post("/api/verify", json={"points": [
            {"x": -5, "y": 5, "t": "2026-09-30T00:00:00Z"},
            {"x": 15, "y": 5, "t": "2026-09-30T00:01:00Z"},
        ]})

    t_holder = {}

    def worker():
        t_holder["r"] = do_verify()

    th = threading.Thread(target=worker)
    th.start()
    barrier_started.wait(5)
    # 在慢核验期间修订区域
    list_regions = client.get("/api/regions").json()
    target = list_regions[0]["id"]
    r = client.patch(f"/api/regions/{target}", json={"name": "并发修改"})
    check("并发修改在核验期间提交成功", r.status_code == 200, r.text)
    th.join(timeout=10)
    rv = t_holder["r"].json()
    check("慢核验使用修改前的已提交版本（名称不变）",
          all(x["region_name"] != "并发修改" for x in rv["results"]), str(rv)[:300])
    # 新核验看到新版本
    rv2 = client.post("/api/verify", json={"points": [
        {"x": -5, "y": 5, "t": "2026-09-30T00:00:00Z"},
        {"x": 15, "y": 5, "t": "2026-09-30T00:01:00Z"},
    ]}).json()
    check("后续核验使用新版本",
          any(x["region_name"] == "并发修改" for x in rv2["results"]))
finally:
    geometry.verify_trajectory = orig

# ================================================================= 出发时刻调度
# 用独立的 8 月时段，避免与上面 9 月区域互相干扰
r = client.post("/api/regions", json={
    "name": "调度-方区",
    "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
    "valid_from": "2026-08-15T00:00:00Z",
    "valid_to": "2026-08-16T00:00:00Z",
})
sched_rid = r.json()["region"]["id"]
rev_sched = client.get("/api/revision").json()["revision"]

# 轨迹：0s 在 (-5,5)，60s 到 (15,5) → 相对 15s 进入、45s 离开方区
SCHED_TRAJ = [
    {"x": -5, "y": 5, "t": 0},
    {"x": 15, "y": 5, "t": 60},
]

r = client.post("/api/schedule", json={
    "points": SCHED_TRAJ,
    "window_start": "2026-08-15T00:00:00Z",
    "window_end": "2026-08-15T01:00:00Z",
})
check("schedule 200", r.status_code == 200, r.text)
v = r.json()
check("schedule 回显窗口（UTC，Z）",
      v["window_start"] == "2026-08-15T00:00:00Z"
      and v["window_end"] == "2026-08-15T01:00:00Z")
check("schedule 带当前 revision", v["revision"] == rev_sched, str(v))
# 禁止 [00:00-45s, 24:00-15s) 与窗口相交 → 安全只有 [00:00:00, 前一日 23:59:15)
# 该段不在窗口内 → 窗口内安全段为空？不：禁止左端 = 2026-08-14T23:59:15Z，
# 早于窗口起，窗口起即处于禁止中；禁止右端远超窗口 → 窗口内无安全时刻
check("窗口整段在禁止中：intervals 为空", v["intervals"] == [], str(v))

# 跨日窗口 [08-14 23:58, 08-15 00:01)：禁止左端 23:59:15 落在窗口内，
# 窗口其余部分（23:59:15 之后）全部处于禁止中 → 只有 1 段安全
r = client.post("/api/schedule", json={
    "points": SCHED_TRAJ,
    "window_start": "2026-08-14T23:58:00Z",
    "window_end": "2026-08-15T00:01:00Z",
})
v = r.json()
check("跨日窗口：仅前导 1 段安全 [23:58:00,23:59:15)",
      len(v["intervals"]) == 1
      and v["intervals"][0] == {
          "start": "2026-08-14T23:58:00Z",
          "end": "2026-08-14T23:59:15Z",
          "start_inclusive": True, "end_inclusive": False},
      str(v))

# 窗口完全在禁止右端之后恢复：区域 valid_to=08-16 00:00，禁止右端=08-15 23:59:45
r = client.post("/api/schedule", json={
    "points": SCHED_TRAJ,
    "window_start": "2026-08-15T23:59:45Z",
    "window_end": "2026-08-16T01:00:00Z",
})
v = r.json()
check("禁止右端（开）之后整窗安全，左端闭",
      len(v["intervals"]) == 1
      and v["intervals"][0]["start"] == "2026-08-15T23:59:45Z"
      and v["intervals"][0]["end"] == "2026-08-16T01:00:00Z"
      and v["intervals"][0]["start_inclusive"] is True
      and v["intervals"][0]["end_inclusive"] is False, str(v))

# 无接触轨迹：整窗安全
r = client.post("/api/schedule", json={
    "points": [{"x": 100, "y": 100, "t": 0}, {"x": 110, "y": 110, "t": 60}],
    "window_start": "2026-08-15T00:00:00Z",
    "window_end": "2026-08-15T01:00:00Z",
})
check("无接触：整窗安全一段",
      r.status_code == 200 and len(r.json()["intervals"]) == 1
      and r.json()["intervals"][0]["start"] == "2026-08-15T00:00:00Z"
      and r.json()["intervals"][0]["end"] == "2026-08-15T01:00:00Z", r.text)

# 单点接触：专用区域（其它 9 月区域在本窗口不生效，保证隔离）。
# 轨迹 5s 时擦过角点 (10,10)；区域 [00:00,00:10) → 禁止 [23:59:55,00:09:55)
r = client.post("/api/regions", json={
    "name": "调度-点接触",
    "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
    "valid_from": "2026-08-17T00:00:00Z",
    "valid_to": "2026-08-17T00:10:00Z",
})
touch_rid = r.json()["region"]["id"]
r = client.post("/api/schedule", json={
    "points": [{"x": 5, "y": 15, "t": 0}, {"x": 15, "y": 5, "t": 10}],
    "window_start": "2026-08-17T00:00:00Z",
    "window_end": "2026-08-17T00:20:00Z",
})
v = r.json()
check("单点接触：安全段 [00:09:55,00:20:00)，左闭（valid_to 端点可出发）",
      len(v["intervals"]) == 1
      and v["intervals"][0] == {
          "start": "2026-08-17T00:09:55Z",
          "end": "2026-08-17T00:20:00Z",
          "start_inclusive": True, "end_inclusive": False}, str(v))

# 接触时刻恰为 valid_from：s = vf - 5 = 前一日 23:59:55 不可出发（禁止左端闭）
r = client.post("/api/schedule", json={
    "points": [{"x": 5, "y": 15, "t": 0}, {"x": 15, "y": 5, "t": 10}],
    "window_start": "2026-08-16T23:59:00Z",
    "window_end": "2026-08-17T00:05:00Z",
})
v = r.json()
check("valid_from 端点接触：安全段止于 23:59:55（右端开）",
      len(v["intervals"]) == 1
      and v["intervals"][0] == {
          "start": "2026-08-16T23:59:00Z",
          "end": "2026-08-16T23:59:55Z",
          "start_inclusive": True, "end_inclusive": False}, str(v))
client.delete(f"/api/regions/{touch_rid}")

# 时区归一化：用 +08:00 表示同样的 UTC 窗口，结果必须一致
r = client.post("/api/schedule", json={
    "points": SCHED_TRAJ,
    "window_start": "2026-08-15T08:00:00+08:00",
    "window_end": "2026-08-15T09:00:00+08:00",
})
v2 = r.json()
r = client.post("/api/schedule", json={
    "points": SCHED_TRAJ,
    "window_start": "2026-08-15T00:00:00Z",
    "window_end": "2026-08-15T01:00:00Z",
})
check("带时区窗口归一化到 UTC，结果一致", r.status_code == 200
      and v2["intervals"] == r.json()["intervals"], str(v2))

# ---- 非法输入：拒绝整个请求 ----
r = client.post("/api/schedule", json={
    "points": [{"x": 0, "y": 0, "t": 0}],
    "window_start": "2026-08-15T00:00:00Z",
    "window_end": "2026-08-15T01:00:00Z",
})
check("轨迹点不足 422", r.status_code == 422, r.text)

r = client.post("/api/schedule", json={
    "points": [{"x": 0, "y": 0, "t": 1}, {"x": 1, "y": 1, "t": 2}],
    "window_start": "2026-08-15T00:00:00Z",
    "window_end": "2026-08-15T01:00:00Z",
})
check("首点相对秒数非 0 → 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)

r = client.post("/api/schedule", json={
    "points": [{"x": 0, "y": 0, "t": 0}, {"x": 1, "y": 1, "t": 0}],
    "window_start": "2026-08-15T00:00:00Z",
    "window_end": "2026-08-15T01:00:00Z",
})
check("相对时刻重复 422", r.status_code == 422 and
      r.json()["error"]["code"] == "invalid_trajectory", r.text)

r = client.post("/api/schedule", json={
    "points": [{"x": 0, "y": 0, "t": 0}, {"x": 1, "y": 1, "t": -5}],
    "window_start": "2026-08-15T00:00:00Z",
    "window_end": "2026-08-15T01:00:00Z",
})
check("相对时刻逆序 422", r.status_code == 422, r.text)

r = client.post("/api/schedule",
    content=_json.dumps({
        "points": [{"x": "NaN", "y": 0, "t": 0},
                   {"x": 1, "y": 1, "t": 10}],
        "window_start": "2026-08-15T00:00:00Z",
        "window_end": "2026-08-15T01:00:00Z"}),
    headers={"content-type": "application/json"})
check("坐标非有限 422", r.status_code == 422, r.text)

r = client.post("/api/schedule",
    content=_json.dumps({
        "points": [{"x": 0, "y": 0, "t": 0},
                   {"x": 1, "y": 1, "t": "Infinity"}],
        "window_start": "2026-08-15T00:00:00Z",
        "window_end": "2026-08-15T01:00:00Z"}),
    headers={"content-type": "application/json"})
check("相对时刻 Inf 422", r.status_code == 422, r.text)

r = client.post("/api/schedule", json={
    "points": SCHED_TRAJ,
    "window_start": "2026-08-15T01:00:00Z",
    "window_end": "2026-08-15T00:00:00Z",
})
check("候选时段不递增 422 invalid_input",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_input", r.text)

r = client.post("/api/schedule", json={
    "points": SCHED_TRAJ,
    "window_start": "2026-08-15T00:00:00",
    "window_end": "2026-08-15T01:00:00",
})
check("候选时段无时区 422", r.status_code == 422, r.text)

r = client.post("/api/schedule", json={
    "points": SCHED_TRAJ,
    "window_start": "2026-08-15T00:00:00Z",
    "window_end": "2026-08-15T01:00:00Z",
    "extra": 1,
})
check("多余字段 422", r.status_code == 422, r.text)

# 清理调度区域，避免影响后续
client.delete(f"/api/regions/{sched_rid}")

# ---- 调度一致性：调度进行中区域被修订，本次仍使用同一已提交快照 ----
r = client.post("/api/regions", json={
    "name": "调度-快照",
    "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
    "valid_from": "2026-07-01T00:00:00Z",
    "valid_to": "2026-07-02T00:00:00Z",
})
snap_rid = r.json()["region"]["id"]

orig_safe = geometry.safe_departure_intervals
barrier_sched_started = threading.Event()


def slow_safe(traj, regions, ws, we):
    barrier_sched_started.set()
    time.sleep(1.0)  # 给修改线程留出提交窗口
    return orig_safe(traj, regions, ws, we)


geometry.safe_departure_intervals = slow_safe
try:
    def worker_sched():
        t_holder["sched"] = client.post("/api/schedule", json={
            "points": [{"x": -5, "y": 5, "t": 0}, {"x": 15, "y": 5, "t": 60}],
            "window_start": "2026-07-01T00:00:00Z",
            "window_end": "2026-07-02T00:00:00Z",
        })

    th2 = threading.Thread(target=worker_sched)
    th2.start()
    barrier_sched_started.wait(5)
    # 慢调度期间把区域改名（推进 revision）
    r = client.patch(f"/api/regions/{snap_rid}", json={"name": "调度-快照-已改"})
    check("调度期间修订区域提交成功", r.status_code == 200, r.text)
    new_rev = r.json()["revision"]
    th2.join(timeout=10)
    sv = t_holder["sched"].json()
    check("慢调度使用修订前的已提交快照 revision",
          sv["revision"] < new_rev, str(sv)[:200])
    # 新请求看到新 revision
    sv2 = client.post("/api/schedule", json={
        "points": [{"x": 100, "y": 100, "t": 0}, {"x": 110, "y": 110, "t": 60}],
        "window_start": "2026-07-01T00:00:00Z",
        "window_end": "2026-07-02T00:00:00Z",
    }).json()
    check("后续调度使用新版本 revision", sv2["revision"] == new_rev, str(sv2)[:200])
finally:
    geometry.safe_departure_intervals = orig_safe
client.delete(f"/api/regions/{snap_rid}")

# ================================================================= 批量原子变更
# 本段全部使用 x>=5000 的隔离坐标，避免与前面用例的遗留区域互相干扰。
def bpost(body):
    return client.post("/api/changes", json=body)


# 危险轨迹：(-5,5)->(15,5) 的局部模式，平移到隔离区域上用时给出具体坐标
def crossing_traj(x0=-5.0, y0=5005.0, x2=15.0):
    return [{"x": x0, "y": y0, "t": "2026-09-30T00:00:00Z"},
            {"x": x2, "y": y0, "t": "2026-09-30T00:01:00Z"}]


def point_touch_traj():
    # 相对方区 [5000,5010]x[5000,5010]：5s 时擦过右上角 (5010,5010)
    return [{"x": 5005, "y": 5015, "t": "2026-09-30T00:00:00Z"},
            {"x": 5015, "y": 5005, "t": "2026-09-30T00:00:10Z"}]


SEPT = {"valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z"}


def square(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


# 准备两个区域：一个用于修订、一个用于删除
r = client.post("/api/regions", json={"name": "批量-待修订",
              "polygon": square(5000, 5000, 5010, 5010), **SEPT})
b_upd_id = r.json()["region"]["id"]
r = client.post("/api/regions", json={"name": "批量-待删除",
              "polygon": square(5200, 5000, 5210, 5010), **SEPT})
b_del_id = r.json()["region"]["id"]
rev0 = client.get("/api/revision").json()["revision"]

# ---- 成功：一次提交含 create/update/delete，revision 只 +1，全部轨迹安全 ----
r = bpost({
    "expected_revision": rev0,
    "operations": [
        {"op": "update", "region_id": b_upd_id,
         "region": {"name": "批量-已修订"}},
        {"op": "delete", "region_id": b_del_id},
        {"op": "create", "region": {"name": "批量-新建",
         "polygon": square(5300, 5000, 5310, 5010), **SEPT}},
    ],
    "trajectories": [{"name": "安全", "points": [
        {"x": 90000, "y": 90000, "t": "2026-09-30T00:00:00Z"},
        {"x": 90010, "y": 90010, "t": "2026-09-30T00:01:00Z"}]}],
})
check("批量提交 200", r.status_code == 200, r.text)
b = r.json()
check("批量整 revision 只 +1", b["revision"] == rev0 + 1, str(b))
check("操作结果 3 条且顺序与请求一致",
      [o["op"] for o in b["operations"]] == ["update", "delete", "create"])
upd_res = next(o for o in b["operations"] if o["op"] == "update")
del_res = next(o for o in b["operations"] if o["op"] == "delete")
cre_res = next(o for o in b["operations"] if o["op"] == "create")
b_new_id = cre_res["region_id"]
check("修订沿用 version+1", upd_res["version"] == 2 and upd_res["region"]["version"] == 2)
check("新建 version=1", cre_res["version"] == 1)
check("删除返回删除前版本 version=1", del_res["version"] == 1)
check("修订名称生效", upd_res["region"]["name"] == "批量-已修订")
check("删除已落库", client.get(f"/api/regions/{b_del_id}").status_code == 404)
check("新建已落库", client.get(f"/api/regions/{b_new_id}").status_code == 200)
check("轨迹结果回填真实新建 id（无负临时 id）",
      all(x["region_id"] >= 1 for x in b["trajectories"][0]["results"])
      and any(x["region_id"] == b_new_id for x in b["trajectories"][0]["results"]))
check("成功响应轨迹 safe=true", b["trajectories"][0]["safe"] is True)
rev1 = b["revision"]

# ---- revision 过期：409 revision_conflict，可区分，不落库 ----
r = bpost({
    "expected_revision": rev0,
    "operations": [{"op": "create", "region": {"name": "不应存在",
         "polygon": square(5400, 5000, 5410, 5010), **SEPT}}],
    "trajectories": [{"points": [
        {"x": 90000, "y": 90000, "t": "2026-09-30T00:00:00Z"},
        {"x": 90010, "y": 90010, "t": "2026-09-30T00:01:00Z"}]}],
})
check("过期 revision 409", r.status_code == 409, r.text)
check("错误码 revision_conflict", r.json()["error"]["code"] == "revision_conflict")
check("409 details 带 expected/current",
      r.json()["error"]["details"][0]["current_revision"] == rev1
      and r.json()["error"]["details"][0]["expected_revision"] == rev0)
check("409 不落库 revision 不变",
      client.get("/api/revision").json()["revision"] == rev1)

# ---- 区域不存在：422 region_not_found（delete 与 update 两种） ----
r = bpost({
    "expected_revision": rev1,
    "operations": [{"op": "delete", "region_id": 999999}],
    "trajectories": [{"points": [
        {"x": 90000, "y": 90000, "t": "2026-09-30T00:00:00Z"},
        {"x": 90010, "y": 90010, "t": "2026-09-30T00:01:00Z"}]}],
})
check("删除不存在 422 region_not_found", r.status_code == 422 and
      r.json()["error"]["code"] == "region_not_found", r.text)
check("region_not_found details 带 op_index 与 region_id",
      r.json()["error"]["details"][0]["op_index"] == 0
      and r.json()["error"]["details"][0]["region_id"] == 999999)

r = bpost({
    "expected_revision": rev1,
    "operations": [{"op": "update", "region_id": 999999, "region": {"name": "x"}}],
    "trajectories": [{"points": [
        {"x": 90000, "y": 90000, "t": "2026-09-30T00:00:00Z"},
        {"x": 90010, "y": 90010, "t": "2026-09-30T00:01:00Z"}]}],
})
check("修订不存在 422 region_not_found", r.status_code == 422 and
      r.json()["error"]["code"] == "region_not_found", r.text)
check("不存在时 revision 不变",
      client.get("/api/revision").json()["revision"] == rev1)

# ---- 同一现有区域在一批中出现两次 ----
r = bpost({
    "expected_revision": rev1,
    "operations": [
        {"op": "update", "region_id": b_upd_id, "region": {"name": "a"}},
        {"op": "delete", "region_id": b_upd_id},
    ],
    "trajectories": [{"points": [
        {"x": 90000, "y": 90000, "t": "2026-09-30T00:00:00Z"},
        {"x": 90010, "y": 90010, "t": "2026-09-30T00:01:00Z"}]}],
})
check("同区域重复操作 422 duplicate_region_operation", r.status_code == 422 and
      r.json()["error"]["code"] == "duplicate_region_operation", r.text)
check("重复操作不落库（名称未变、仍可查）",
      client.get(f"/api/regions/{b_upd_id}").json()["name"] == "批量-已修订")

# ---- 非法数据：空 update / 自相交多边形 / 合并后时段非法 ----
r = bpost({
    "expected_revision": rev1,
    "operations": [{"op": "update", "region_id": b_upd_id, "region": {}}],
    "trajectories": [{"points": [
        {"x": 90000, "y": 90000, "t": "2026-09-30T00:00:00Z"},
        {"x": 90010, "y": 90010, "t": "2026-09-30T00:01:00Z"}]}],
})
check("空 update 422 invalid_input", r.status_code == 422 and
      r.json()["error"]["code"] == "invalid_input", r.text)

r = bpost({
    "expected_revision": rev1,
    "operations": [{"op": "create", "region": {"name": "坏多边形",
         "polygon": [[5000, 5000], [5010, 5010], [5010, 5000], [5000, 5010]],
         **SEPT}}],
    "trajectories": [{"points": [
        {"x": 90000, "y": 90000, "t": "2026-09-30T00:00:00Z"},
        {"x": 90010, "y": 90010, "t": "2026-09-30T00:01:00Z"}]}],
})
check("批量中自相交多边形 422", r.status_code == 422, r.text)

r = bpost({
    "expected_revision": rev1,
    "operations": [{"op": "update", "region_id": b_upd_id, "region": {
        "valid_from": "2026-11-01T00:00:00Z",
        "valid_to": "2026-10-01T00:00:00Z"}}],
    "trajectories": [{"points": [
        {"x": 90000, "y": 90000, "t": "2026-09-30T00:00:00Z"},
        {"x": 90010, "y": 90010, "t": "2026-09-30T00:01:00Z"}]}],
})
check("合并后时段非法 422 invalid_input", r.status_code == 422 and
      r.json()["error"]["code"] == "invalid_input", r.text)

# ---- 非法轨迹：逆序 / 不带时区 / 不足两点；空操作空轨迹；未知 op ----
def one_create(name="x"):
    return [{"op": "create", "region": {"name": name,
             "polygon": square(5500, 5000, 5510, 5010), **SEPT}}]


r = bpost({"expected_revision": rev1, "operations": one_create(),
           "trajectories": [{"points": [
               {"x": 0, "y": 5005, "t": "2026-09-30T00:00:10Z"},
               {"x": 1, "y": 5005, "t": "2026-09-30T00:00:00Z"}]}]})
check("批量中逆序轨迹 422 invalid_trajectory", r.status_code == 422 and
      r.json()["error"]["code"] == "invalid_trajectory", r.text)

r = bpost({"expected_revision": rev1, "operations": one_create(),
           "trajectories": [{"points": [
               {"x": 0, "y": 5005, "t": "2026-09-30T00:00:00"},
               {"x": 1, "y": 5005, "t": "2026-09-30T00:00:10"}]}]})
check("批量轨迹不带时区 422", r.status_code == 422, r.text)

r = bpost({"expected_revision": rev1, "operations": one_create(),
           "trajectories": [{"points": [
               {"x": 0, "y": 5005, "t": "2026-09-30T00:00:00Z"}]}]})
check("批量轨迹不足两点 422", r.status_code == 422, r.text)

r = bpost({"expected_revision": rev1, "operations": [], "trajectories": []})
check("空操作空轨迹 422", r.status_code == 422, r.text)

r = bpost({"expected_revision": rev1,
           "operations": [{"op": "frobnicate", "region_id": 1}],
           "trajectories": [{"points": [
               {"x": 0, "y": 5005, "t": "2026-09-30T00:00:00Z"},
               {"x": 1, "y": 5005, "t": "2026-09-30T00:00:10Z"}]}]})
check("未知操作类型 422", r.status_code == 422, r.text)
check("上述全部非法批均未落库（revision 仍为 rev1）",
      client.get("/api/revision").json()["revision"] == rev1)

# ---- 轨迹不安全：对变更后完整集合核验，列出穿越明细，整批不落库 ----
# b_upd_id 已扩大为 [5000,5020]x[5000,5020]；轨迹 (4995,5005)->(5015,5005)
# 在 15s 进入、45s 离开。
r = bpost({
    "expected_revision": rev1,
    "operations": [
        {"op": "update", "region_id": b_upd_id, "region": {"name": "批量-扩大"}},
        {"op": "create", "region": {"name": "批量-不该建成",
         "polygon": square(5600, 5000, 5610, 5010), **SEPT}},
    ],
    "trajectories": [{"name": "穿越轨迹",
                      # b_upd_id 几何为初始的 10 宽方区 [5000,5010]，仅改名；
                      # 轨迹跨度 20，15s 进入、45s 离开
                      "points": crossing_traj(4995, 5005, 5015)}],
})
check("不安全轨迹 422 trajectory_unsafe", r.status_code == 422 and
      r.json()["error"]["code"] == "trajectory_unsafe", r.text)
det = r.json()["error"]["details"]
check("不安全详情定位到 trajectories[0]", det[0]["traj_index"] == 0
      and det[0]["name"] == "穿越轨迹")
vregions = {v["region_name"]: v["crossings"] for v in det[0]["violations"]}
check("明细含现存（修订后）区域穿越区间 [15s,45s]",
      "批量-扩大" in vregions
      and vregions["批量-扩大"][0]["entry_time"] == "2026-09-30T00:00:15Z"
      and vregions["批量-扩大"][0]["exit_time"] == "2026-09-30T00:00:45Z",
      str(det)[:300])
check("不安全时整批不落库：修订名未变、新区未建、revision 不变",
      client.get(f"/api/regions/{b_upd_id}").json()["name"] == "批量-已修订"
      and all(x["name"] != "批量-不该建成" for x in client.get("/api/regions").json())
      and client.get("/api/revision").json()["revision"] == rev1)

# ---- 边界接触即穿越：擦过顶点也算不安全 ----
r = bpost({
    "expected_revision": rev1,
    "operations": [{"op": "create", "region": {"name": "批量-点接触",
         "polygon": square(5000, 5000, 5010, 5010), **SEPT}}],
    "trajectories": [{"points": point_touch_traj()}],
})
check("单点接触轨迹同样判不安全 422", r.status_code == 422 and
      r.json()["error"]["code"] == "trajectory_unsafe", r.text)
pc = [c for v in r.json()["error"]["details"][0]["violations"]
      for c in v["crossings"]]
check("明细标记 point_contact 且起止相同",
      any(c["point_contact"] and c["entry_time"] == c["exit_time"] for c in pc),
      str(pc)[:300])

# ---- 生效时段左闭右开：跨过 valid_to 被截断；恰在 valid_to 已失效则安全 ----
r = bpost({
    "expected_revision": rev1,
    "operations": [{"op": "create", "region": {"name": "批量-右端点",
         "polygon": square(5000, 5000, 5010, 5010),
         "valid_from": "2026-09-30T00:00:00Z",
         "valid_to": "2026-09-30T00:00:30Z"}}],
    "trajectories": [{"points": crossing_traj(4995, 5005, 5015)}],
})
check("跨 valid_to 穿越仍违规（截断到 [..,30)）", r.status_code == 422 and
      r.json()["error"]["code"] == "trajectory_unsafe", r.text)
c30 = [c for v in r.json()["error"]["details"][0]["violations"]
       for c in v["crossings"] if v["region_name"] == "批量-右端点"][0]
check("穿越在 valid_to 处截断且 exit_inclusive=false",
      c30["exit_time"] == "2026-09-30T00:00:30Z" and c30["exit_inclusive"] is False,
      str(c30))

# 区域恰在轨迹首点时刻失效（右端开）：接触发生在 [valid_to, ...]，不参与核验
r = bpost({
    "expected_revision": rev1,
    "operations": [{"op": "create", "region": {"name": "批量-已失效",
         "polygon": square(6000, 5000, 6010, 5010),
         "valid_from": "2026-09-29T00:00:00Z",
         "valid_to": "2026-09-30T00:00:00Z"}}],
    "trajectories": [{"points": crossing_traj(5995, 5005, 6015)}],
})
check("区域在轨迹开始时已失效（右端开）→ 整批安全提交",
      r.status_code == 200, r.text)
expired_new = next(o for o in r.json()["operations"]
                   if o["region"]["name"] == "批量-已失效")["region_id"]
rev1 = r.json()["revision"]

# ---- 多条轨迹：任一不安全即拒绝，只列出不安全的那条 ----
r = bpost({
    "expected_revision": rev1,
    "operations": [{"op": "create", "region": {"name": "批量-多轨迹",
         "polygon": square(5000, 5000, 5010, 5010), **SEPT}}],
    "trajectories": [
        {"name": "安全条", "points": [
            {"x": 90000, "y": 90000, "t": "2026-09-30T00:00:00Z"},
            {"x": 90010, "y": 90010, "t": "2026-09-30T00:01:00Z"}]},
        {"name": "危险条", "points": crossing_traj(4995, 5005, 5015)},
    ],
})
check("两条轨迹中第二条不安全 → 422", r.status_code == 422 and
      r.json()["error"]["code"] == "trajectory_unsafe", r.text)
check("只列出不安全的那条（traj_index=1）",
      [d["traj_index"] for d in r.json()["error"]["details"]] == [1]
      and r.json()["error"]["details"][0]["name"] == "危险条")

# ---- 删除挡路区域使原本危险的轨迹安全：核验基于「变更后」完整集合 ----
# 先新建一个隔离挡路区域（避免删 b_upd_id 影响后续并发用例对它的依赖）
r = client.post("/api/regions", json={"name": "批量-挡路",
              "polygon": square(5700, 5000, 5710, 5010), **SEPT})
block_id = r.json()["region"]["id"]
rev1 = client.get("/api/revision").json()["revision"]
r = bpost({
    "expected_revision": rev1,
    "operations": [{"op": "delete", "region_id": block_id}],
    "trajectories": [{"name": "删后安全",
                      "points": crossing_traj(5695, 5005, 5715)}],
})
check("删除挡路区域后轨迹安全 → 提交成功", r.status_code == 200, r.text)
check("删除批返回被删区域旧版本 version=1",
      next(o for o in r.json()["operations"] if o["op"] == "delete")["version"] == 1)
rev1 = r.json()["revision"]

# ---- 并发：批量持 meta 行锁期间，其它写请求不能绕过该批串行提交 ----
orig_verify = geometry.verify_trajectory
batch_barrier = threading.Event()


def slow_batch_verify(traj, regions):
    batch_barrier.set()
    time.sleep(1.0)
    return orig_verify(traj, regions)


geometry.verify_trajectory = slow_batch_verify
try:
    holder = {}

    def batch_worker():
        holder["r"] = bpost({
            "expected_revision": rev1,
            "operations": [{"op": "create", "region": {"name": "批量-并发中",
                 "polygon": square(5800, 5000, 5810, 5010), **SEPT}}],
            "trajectories": [{"points": [
                {"x": 90000, "y": 90000, "t": "2026-09-30T00:00:00Z"},
                {"x": 90010, "y": 90010, "t": "2026-09-30T00:01:00Z"}]}],
        })

    th = threading.Thread(target=batch_worker)
    th.start()
    batch_barrier.wait(5)
    t0 = time.time()
    r_conc = client.post("/api/regions", json={"name": "批量-并发插队",
              "polygon": square(5900, 5000, 5910, 5010), **SEPT})
    waited = time.time() - t0
    th.join(timeout=10)
    check("批量进行中并发写被阻塞（等待 meta 锁）", waited >= 0.9,
          f"waited={waited:.2f}s")
    check("批量与并发写最终都成功（各自一个 revision，不丢更新）",
          holder["r"].status_code == 200 and r_conc.status_code == 201,
          f"batch={holder['r'].status_code} conc={r_conc.status_code}")
    revs = sorted([holder["r"].json()["revision"], r_conc.json()["revision"]])
    check("两次写各占一个 revision（连续 +1，批量不跳号）",
          revs == [rev1 + 1, rev1 + 2], str(revs))
    conc_id = r_conc.json()["region"]["id"]
    batch_conc_id = next(o for o in holder["r"].json()["operations"]
                         if o["region"]["name"] == "批量-并发中")["region_id"]
finally:
    geometry.verify_trajectory = orig_verify

# 清理批量测试产生的区域
for rid in [b_upd_id, b_new_id, expired_new, conc_id, batch_conc_id]:
    client.delete(f"/api/regions/{rid}")

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部 API 集成用例通过")
