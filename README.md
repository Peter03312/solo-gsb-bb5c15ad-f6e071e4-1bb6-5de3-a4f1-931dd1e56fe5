# 临时限行核验服务

面向场地安全员的临时限行区域登记、轨迹穿越核验与安全出发时刻调度服务。
安全员在平面直角坐标系中登记带 UTC 生效时段的简单多边形区域；系统对按时刻
严格递增、相邻点直线匀速运动的连续轨迹做整段核验，返回每个区域的穿越起止
时刻以及总体是否安全；也可以对一条固定行程（相对秒数轨迹）在带时区的候选
出发时段内，精确求出所有不会触碰限行区的出发区间。多处区域还可通过
`POST /api/changes` 携带预期 revision 做一次原子批量变更，并保证指定通行
轨迹在变更后的完整区域集合上仍然安全。

## 核验规则

* **轨迹**：至少 2 个点 `(x, y, t)`；时刻必须严格递增（重复/逆序报错且不处理）；
  相邻点之间按**直线匀速**运动。
* **交点插值**：交点时刻通过线段参数插值得到，**不做采样点检查**——即使所有提交
  的采样点都在区域外，只要两点连线穿过区域就会被检出。
* **边界即穿越**：沿边界运动、与顶点单点接触都算穿越；单点接触返回
  `entry_time == exit_time` 的退化区间，并置 `point_contact: true`。
* **区间合并**：同一区域相接（端点相同）或重叠的穿越段合并为一段；
  不同区域分别列出。
* **生效时段左闭右开**：区域仅在 `[valid_from, valid_to)` 内参与核验。
  * 接触时刻恰为 `valid_from`：参与（左端闭）；
  * 接触时刻恰为 `valid_to`：不参与（右端开，此时区域已失效）；
  * 正长度穿越跨过 `valid_to` 时，区间在 `valid_to` 处截断，
    `exit_inclusive=false`。
* **非法输入不保存**：非法多边形（自相交、退化、面积为 0）、非有限坐标
  （NaN/Infinity）、`valid_from >= valid_to` 均返回 422 且不落库。
* **一致性快照**：一次核验在单个 `REPEATABLE READ READ ONLY` 事务中读取区域集合，
  即使核验期间有区域被并发登记/修订/删除，本次核验仍使用同一已提交版本；
  响应中的 `revision` 标明所用集合版本。

## 出发时刻调度规则（`POST /api/schedule`）

* **输入**：固定行程用**相对秒数**给出 `{"x","y","t"}`，首点相对秒数必须为 `0`，
  其余时刻严格递增；候选出发时段 `[window_start, window_end)` 带时区，服务端
  归一化为 UTC。相邻点之间仍按直线匀速运动。
* **精确区间运算，不做离散抽样**：对每个区域，先求轨迹与多边形（**含边界**）
  的全部几何接触，得到相对时刻闭区间 `[a, b]`（单点接触时 `a == b`）。
  设出发时刻为 `s`、区域生效窗口为 `[vf, vt)`，则该接触造成违规当且仅当
  `存在 τ∈[a,b] 使 vf ≤ s+τ < vt`，等价于
  **`s ∈ [vf-b, vt-a)`（左闭右开）**。该公式对正长度接触与单点接触统一成立。
* **端点语义准确**：
  * 恰在 `valid_from` 接触 → 对应出发时刻禁止（禁止区间左端闭）；
  * 恰在 `valid_to` 接触 → 对应出发时刻安全（禁止区间右端开）；
  * 沿边界整段运动按闭区间 `[a,b]` 参与计算，边界即违规；
  * 各区域禁止区间取并集（相接或重叠即合并），再从候选窗口（左闭右开）中扣除。
* **输出**：按起点排序的安全出发区间，端点统一为 UTC ISO8601 并带
  `start_inclusive/end_inclusive` 开闭标记（当前规则下恒为左闭右开）；
  无任何可行时刻时返回 `"intervals": []`。响应带 `revision`。
* **拒绝整个请求（422）**：轨迹点少于 2 个；坐标或相对时刻非有限
  （NaN/Infinity）；首点相对秒数不为 0 或相对时刻不严格递增；
  `window_start >= window_end`；候选时段不带时区。
* **一致性快照**：与 `/api/verify` 相同，整个计算在单个
  `REPEATABLE READ READ ONLY` 事务中完成，区域并发修订时本请求使用同一已提交
  快照。

## 技术栈

Python 3.11 · FastAPI · PostgreSQL 16 · Shapely 2（GEOS）· psycopg3 · Uvicorn

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000 | 服务根路径（端点列表） |
| http://localhost:8000/docs | Swagger UI（可在线调用） |
| http://localhost:8000/openapi.json | OpenAPI 描述 |
| http://localhost:8000/health | 健康检查 |
| PostgreSQL | localhost:5432（用户/库/密码均为 restrict） |

首次启动时应用会自动建表（`regions`、`region_meta`）并初始化全局版本号，
无需手工执行 SQL。数据库未就绪时应用会自动重试连接（默认 30 次、间隔 1 秒）。

停止并清理数据卷：

```bash
docker compose down -v
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://restrict:restrict@localhost:5432/restrict` | PostgreSQL DSN |
| `API_PORT` | `8000` | Compose 映射到宿主机的 API 端口 |
| `POSTGRES_PORT` | `5432` | Compose 映射到宿主机的数据库端口 |
| `DB_CONNECT_RETRIES` | `30` | 启动时连接数据库的重试次数 |
| `DB_CONNECT_INTERVAL` | `1.0` | 重试间隔（秒） |

例如改用 18000 端口：`API_PORT=18000 docker compose up --build`。

## 本地（无 Docker）运行

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://restrict:restrict@localhost:5432/restrict"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## HTTP API

时间字段一律使用带时区的 ISO8601，建议统一 `...Z`（UTC）。
多边形顶点按 `[[x, y], ...]` 提交，首尾点可显式闭合也可不闭合；
服务端校验为简单多边形后归一化为逆时针闭合环存储。

### 区域管理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/regions` | 登记区域（201） |
| GET | `/api/regions` | 列出全部区域 |
| GET | `/api/regions/{id}` | 查询单个区域 |
| PATCH | `/api/regions/{id}` | 修订区域（字段任意组合，部分更新）；`version` +1 |
| DELETE | `/api/regions/{id}` | 删除区域 |
| GET | `/api/revision` | 当前区域集合全局版本号（任意写操作后单调 +1） |
| POST | `/api/changes` | **批量原子变更**：一次提交多处登记/修订/删除并核验轨迹 |
| POST | `/api/verify` | 对一条绝对时刻轨迹做整段穿越核验 |
| POST | `/api/schedule` | 对一条相对秒数固定行程，求候选时段内全部安全出发区间 |

登记示例：

```bash
curl -X POST http://localhost:8000/api/regions \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "A区-焊接作业面",
        "polygon": [[0,0],[100,0],[100,100],[0,100]],
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to":   "2026-10-01T00:00:00Z"
      }'
```

### 批量原子变更 `POST /api/changes`

安全员把多处限行区域的**登记 / 修订 / 删除**连同必须继续安全的通行轨迹，
作为一次变更提交。请求体：

* `expected_revision`：提交方预期的当前全局 revision（先 `GET /api/revision`
  获取）。服务端在写事务内锁定版本行后比对，过期则整批拒绝（`409`）。
* `operations`：至少 1 条，顺序即响应中结果的顺序；三类操作：
  * `{"op":"create","region":{...登记字段...}}`
  * `{"op":"update","region_id":12,"region":{...任意部分字段...}}`
  * `{"op":"delete","region_id":12}`
  * **同一现有区域在一批中只能出现一次**（无论 update 还是 delete）；
    update 的 `region` 至少要给一个字段。
* `trajectories`：至少 1 条带时区的**绝对时刻**轨迹（结构与 `/api/verify`
  的 `points` 相同，可带 `name`）。

服务端对「**变更后**」的完整区域集合，按直线匀速、边界接触即穿越、生效时段
左闭右开的同一套规则核验全部轨迹。**仅当 revision 匹配、操作均合法且全部
轨迹安全时才原子落库**：修订沿用 `version+1`，整批全局 `revision` 只递增
一次。任一条件不满足都整体回滚、不留任何中间状态。

请求示例：

```bash
curl -X POST http://localhost:8000/api/changes \
  -H 'Content-Type: application/json' \
  -d '{
        "expected_revision": 3,
        "operations": [
          {"op": "update", "region_id": 1,
           "region": {"name": "A区-扩大", "polygon": [[0,0],[120,0],[120,120],[0,120]]}},
          {"op": "delete", "region_id": 2},
          {"op": "create", "region": {
              "name": "E区-新增",
              "polygon": [[200,0],[260,0],[260,60],[200,60]],
              "valid_from": "2026-09-01T00:00:00Z",
              "valid_to":   "2026-10-01T00:00:00Z"}}
        ],
        "trajectories": [
          {"name": "远场安全往返", "points": [
            {"x": 5000, "y": 5000, "t": "2026-09-30T00:00:00Z"},
            {"x": 5100, "y": 5100, "t": "2026-09-30T00:01:00Z"}
          ]}
        ]
      }'
```

成功响应（`200`）：`revision` 为新全局版本；`operations` 与请求同序，
新建结果带回 `region_id`/`version`，修订带回修订后 `version`，删除带回
删除前的最后 `version`；`trajectories` 给出每条轨迹在变更后集合上的逐区域
核验结果（提交成功时 `safe` 恒为 `true`）：

```json
{
  "revision": 4,
  "operations": [
    {"op": "update", "op_index": 0, "region_id": 1, "version": 3, "region": {"..." : "修订后完整区域对象"}},
    {"op": "delete", "op_index": 1, "region_id": 2, "version": 5},
    {"op": "create", "op_index": 2, "region_id": 11, "version": 1, "region": {"..." : "新建完整区域对象"}}
  ],
  "trajectories": [
    {"traj_index": 0, "name": "远场安全往返", "safe": true, "results": [{"..." : "与 /api/verify 同构"}]}
  ]
}
```

失败时**均不落库**，错误码可区分原因：

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 409 | `revision_conflict` | `expected_revision` 与当前不一致；`details` 带 `expected_revision/current_revision` |
| 422 | `region_not_found` | update/delete 引用的区域不存在；`details` 带 `op_index/region_id` |
| 422 | `duplicate_region_operation` | 同一现有区域在一批中出现多次 |
| 422 | `invalid_input` | update 未给任何字段、新建/合并后 `valid_from >= valid_to` 等 |
| 422 | `invalid_polygon` / `validation_error` | 多边形非法、坐标非有限、结构不符等 |
| 422 | `invalid_trajectory` | 轨迹时刻不严格递增、坐标非有限等 |
| 422 | `trajectory_unsafe` | 变更后集合上存在不安全轨迹；`details` 逐条列出 `traj_index/name` 与逐区域穿越明细（含 `entry_time/exit_time`、`point_contact`、`exit_inclusive` 等） |

并发语义：所有写事务先对单行 `region_meta` 取行锁再操作，因此多个
`/api/changes` 及单区域登记/修订/删除彼此**串行化**、整批 revision 不重号
不跳号；涉及的区域行按 id 升序加 `FOR UPDATE`，避免丢失更新与死锁。
READ COMMITTED 下若某行已被并发写者修改/删除，本事务会等待并基于其**已提交
结果**重新核验，因此并发区域变更无法绕过轨迹安全校验；万一数据库检测到
死锁，返回 422 提示按最新 revision 重试。

### 轨迹核验 `POST /api/verify`

请求：

```json
{
  "points": [
    {"x": -50, "y": 50, "t": "2026-09-30T00:00:00Z"},
    {"x": 150, "y": 50, "t": "2026-09-30T00:01:00Z"}
  ]
}
```

响应（该轨迹在 00:00:15 进入、00:00:45 离开 0~100 的区域，时刻由插值得到）：

```json
{
  "safe": false,
  "revision": 1,
  "checked_at": "2026-09-30T03:40:00.000000Z",
  "results": [
    {
      "region_id": 1,
      "region_name": "A区-焊接作业面",
      "crossings": [
        {
          "entry_time": "2026-09-30T00:00:15Z",
          "exit_time": "2026-09-30T00:00:45Z",
          "entry_inclusive": true,
          "exit_inclusive": true,
          "point_contact": false
        }
      ]
    }
  ]
}
```

* `safe=true` 当且仅当所有区域的 `crossings` 均为空。
* `entry_inclusive/exit_inclusive` 标明端点开闭；穿越被生效窗口右端截断时
  `exit_inclusive=false`。
* 无穿越的区域也会出现在 `results` 中（`crossings: []`），保证逐区域可核对。

### 出发时刻调度 `POST /api/schedule`

请求：固定行程用相对首点的秒数（首点 `t=0`、严格递增），候选时段带时区：

```json
{
  "points": [
    {"x": -50, "y": 50, "t": 0},
    {"x": 150, "y": 50, "t": 60}
  ],
  "window_start": "2026-08-31T23:58:00Z",
  "window_end":   "2026-09-01T00:02:00Z"
}
```

假设某区域为 `[0,100]×[0,100]`、仅在 `2026-09-01T00:00:00Z` 至
`2026-09-01T00:01:00Z` 生效，则轨迹在相对 `15s~45s` 与区域接触（含边界），
禁止出发区间为 `[vf-45s, vt-15s) = [2026-08-31T23:59:15Z, 2026-09-01T00:00:45Z)`，
候选窗口被切成两段安全区间：

```json
{
  "revision": 1,
  "window_start": "2026-08-31T23:58:00Z",
  "window_end": "2026-09-01T00:02:00Z",
  "intervals": [
    {
      "start": "2026-08-31T23:58:00Z",
      "end": "2026-08-31T23:59:15Z",
      "start_inclusive": true,
      "end_inclusive": false
    },
    {
      "start": "2026-09-01T00:00:45Z",
      "end": "2026-09-01T00:02:00Z",
      "start_inclusive": true,
      "end_inclusive": false
    }
  ]
}
```

* 区间端点为 UTC，恒为左闭右开；`intervals` 为空表示候选时段内无安全出发时刻。
* 时区处理：`window_start/window_end` 可提交任意带时区的 ISO8601（如
  `2026-09-01T08:00:00+08:00`），服务端归一化为 UTC；不带时区返回 422。
* 几何接触为单点时（如擦过顶点），接触区间退化为 `[a,a]`，禁止区间长度恰为
  区域生效窗口长度；接触恰落在 `valid_to` 的那次出发安全（右端开），
  落在 `valid_from` 的那次出发禁止（左端闭）。

### 错误响应

统一为 `422`（语义错误）、`409`（批量提交时 revision 过期）或 `404`
（单区域接口区域不存在）。除通用的 `validation_error` 外，轨迹类错误使用
专用错误码：`/api/verify` 为 `invalid_trajectory`，`/api/schedule` 的相对
轨迹问题同样返回 `invalid_trajectory`，候选时段不递增返回 `invalid_input`；
`/api/changes` 另用 `revision_conflict`（409）、`region_not_found`、
`duplicate_region_operation`、`trajectory_unsafe` 等可区分错误码（详见上节）：

```json
{
  "error": {
    "code": "validation_error",
    "message": "请求参数校验失败",
    "details": [{"loc": ["body", "polygon"], "message": "...", "type": "..."}]
  }
}
```

## 调用示例

```bash
# 端到端 curl 示例：安全出发区间（含单点接触/生效端点）、登记区域、
# 核验穿越、修订与删除、批量原子变更（/api/changes）
bash examples/curl_examples.sh

# 仅用 Python 标准库的调用示例（含调度与批量变更/不安全拒绝演示）
python3 examples/python_client.py
```

## 测试

```bash
# 纯几何引擎：穿越核验用例，不需要数据库
python3 tests/test_geometry.py

# 纯几何引擎：安全出发区间调度用例（精确端点、单点接触、沿边界、
# 生效窗口端点、多区域并集等），不需要数据库
python3 tests/test_schedule_geometry.py

# HTTP + PostgreSQL 集成测试（需要可连接的数据库，会清空 regions 表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_api.py
```

## 项目结构

```
app/
  main.py        # FastAPI 路由、事务边界、错误处理（verify/schedule/changes）
  schemas.py     # Pydantic 请求/响应模型与输入校验（含批量操作判别联合）
  geometry.py    # 穿越核验与安全出发区间引擎（插值、裁剪、合并、区间差）
  repository.py  # 区域 CRUD、一致性快照读取、批量原子变更（加锁/构造/落库）
  db.py          # psycopg3 连接池、建表初始化、启动重试
examples/        # curl 与 Python 调用示例（含调度）
tests/           # 几何离线测试与 API 集成测试
Dockerfile, docker-compose.yml
```
