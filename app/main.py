"""FastAPI 入口：临时限行核验 HTTP API。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.errors import CheckViolation, DeadlockDetected, SerializationFailure
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, geometry, repository
from .schemas import (
    BatchChangeRequest,
    BatchChangeResponse,
    RegionCreate,
    RegionCreatedResponse,
    RegionOut,
    RegionResultOut,
    RegionUpdate,
    RevisionOut,
    ScheduleRequest,
    ScheduleResponse,
    TrajectoryCheckOut,
    VerifyRequest,
    VerifyResponse,
    _iso_z,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("restriction-service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_pool()
    yield
    db.close_pool()


app = FastAPI(
    title="临时限行核验服务",
    version="1.2.0",
    description="登记平面多边形限行区域、对连续轨迹进行穿越核验，计算安全出发区间，"
                "并支持携带预期 revision 的多区域原子批量变更与变更后轨迹核验。",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 错误处理

class DomainError(Exception):
    """业务语义错误（HTTP 422）。"""

    def __init__(self, message: str, loc: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.loc = loc or []


class RevisionConflict(Exception):
    """expected_revision 与当前全局 revision 不一致（HTTP 409）。"""

    def __init__(self, expected: int, current: int):
        self.expected = expected
        self.current = current


class RegionNotFoundInBatch(Exception):
    """批量变更引用了不存在的区域（HTTP 422，区别于 revision 过期）。"""

    def __init__(self, region_id: int, op_index: int):
        self.region_id = region_id
        self.op_index = op_index


class DuplicateRegionInBatch(Exception):
    """同一现有区域在一批中出现了多次（HTTP 422）。"""

    def __init__(self, region_id: int, op_index: int):
        self.region_id = region_id
        self.op_index = op_index


class TrajectoryUnsafe(Exception):
    """变更后区域集合上存在不安全轨迹（HTTP 422，整批不落库）。"""

    def __init__(self, details: list[dict]):
        self.details = details


def _error_response(status: int, code: str, message: str, details=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or [],
            }
        },
    )


@app.exception_handler(ValidationError)
async def _on_pydantic_error(_request: Request, exc: ValidationError):
    details = []
    for err in exc.errors():
        details.append({
            "loc": [str(x) for x in err.get("loc", [])],
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        })
    return _error_response(422, "validation_error", "请求参数校验失败", details)


@app.exception_handler(RequestValidationError)
async def _on_request_validation_error(_request: Request, exc: RequestValidationError):
    details = []
    for err in exc.errors():
        details.append({
            "loc": [str(x) for x in err.get("loc", [])],
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        })
    return _error_response(422, "validation_error", "请求参数校验失败", details)


@app.exception_handler(StarletteHTTPException)
async def _on_http_exception(_request: Request, exc: StarletteHTTPException):
    # 404/405 等也使用统一错误信封；业务侧已把 {"error": {...}} 放在 detail 中
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        body = exc.detail
    else:
        body = {"error": {"code": "http_error", "message": str(exc.detail), "details": []}}
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(DomainError)
async def _on_domain_error(_request: Request, exc: DomainError):
    details = [{"loc": exc.loc, "message": exc.message}] if exc.loc else []
    return _error_response(422, "invalid_input", exc.message, details)


@app.exception_handler(geometry.RegionError)
async def _on_region_error(_request: Request, exc: geometry.RegionError):
    return _error_response(422, "invalid_polygon", str(exc))


@app.exception_handler(geometry.TrajectoryError)
async def _on_trajectory_error(_request: Request, exc: geometry.TrajectoryError):
    return _error_response(422, "invalid_trajectory", str(exc))


@app.exception_handler(RevisionConflict)
async def _on_revision_conflict(_request: Request, exc: RevisionConflict):
    return _error_response(
        409,
        "revision_conflict",
        f"expected_revision={exc.expected} 已过期，当前 revision={exc.current}",
        [{"expected_revision": exc.expected, "current_revision": exc.current}],
    )


@app.exception_handler(RegionNotFoundInBatch)
async def _on_region_not_found_in_batch(_request: Request, exc: RegionNotFoundInBatch):
    return _error_response(
        422,
        "region_not_found",
        f"operations[{exc.op_index}] 引用的区域 {exc.region_id} 不存在",
        [{"loc": ["body", "operations", exc.op_index, "region_id"],
          "op_index": exc.op_index, "region_id": exc.region_id}],
    )


@app.exception_handler(DuplicateRegionInBatch)
async def _on_duplicate_region_in_batch(_request: Request, exc: DuplicateRegionInBatch):
    return _error_response(
        422,
        "duplicate_region_operation",
        f"区域 {exc.region_id} 在同一批中出现了多次（operations[{exc.op_index}]）",
        [{"loc": ["body", "operations", exc.op_index, "region_id"],
          "op_index": exc.op_index, "region_id": exc.region_id}],
    )


@app.exception_handler(repository.PlanValidationError)
async def _on_plan_validation_error(_request: Request, exc: repository.PlanValidationError):
    return _error_response(422, "invalid_input", str(exc))


@app.exception_handler(TrajectoryUnsafe)
async def _on_trajectory_unsafe(_request: Request, exc: TrajectoryUnsafe):
    return _error_response(
        422,
        "trajectory_unsafe",
        "存在轨迹在变更后的区域集合上不安全，整批变更未提交",
        exc.details,
    )


# ---------------------------------------------------------------- 区域管理

@app.post("/api/regions", response_model=RegionCreatedResponse, status_code=201,
          tags=["regions"])
def create_region(payload: RegionCreate):
    """登记一个限行区域；非法多边形或不递增时段返回 422 且不保存。"""
    try:
        payload.validate_time_order()
    except ValueError as exc:
        raise DomainError(str(exc), ["valid_from", "valid_to"]) from exc
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                region, revision = repository.create_region(cur, payload, payload.polygon)
            except CheckViolation as exc:
                raise DomainError("生效时段必须满足 valid_from < valid_to（左闭右开）") from exc
        conn.commit()
    return RegionCreatedResponse(region=RegionOut.model_validate(region), revision=revision)


@app.get("/api/regions", response_model=list[RegionOut], tags=["regions"])
def list_regions():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            rows = repository.list_regions(cur)
    return [RegionOut.model_validate(r) for r in rows]


@app.get("/api/regions/{region_id}", response_model=RegionOut, tags=["regions"])
def get_region(region_id: int = Path(ge=1)):
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            cur.execute(
                """SELECT id, name, polygon, valid_from, valid_to, version,
                          created_at, updated_at
                   FROM regions WHERE id = %s""",
                (region_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                               "message": f"区域 {region_id} 不存在"}})
    return RegionOut.model_validate(row)


@app.patch("/api/regions/{region_id}", response_model=RegionCreatedResponse, tags=["regions"])
def update_region(payload: RegionUpdate, region_id: int = Path(ge=1)):
    """修订区域（部分更新）；每次修订 version 加 1 并推进全局 revision。"""
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                result = repository.update_region(
                    cur, region_id, payload, payload.polygon
                )
            except ValueError as exc:
                raise DomainError(str(exc), ["valid_from", "valid_to"]) from exc
            except CheckViolation as exc:
                raise DomainError("生效时段必须满足 valid_from < valid_to（左闭右开）") from exc
            if result is None:
                raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                                       "message": f"区域 {region_id} 不存在"}})
            region, revision = result
        conn.commit()
    return RegionCreatedResponse(region=RegionOut.model_validate(region), revision=revision)


@app.delete("/api/regions/{region_id}", response_model=RevisionOut, tags=["regions"])
def remove_region(region_id: int = Path(ge=1)):
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            deleted, revision = repository.delete_region(cur, region_id)
            if not deleted:
                raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                                       "message": f"区域 {region_id} 不存在"}})
        conn.commit()
    return RevisionOut(revision=revision)


@app.get("/api/revision", response_model=RevisionOut, tags=["regions"])
def current_revision():
    """当前区域集合版本号；每次登记/修订/删除后单调递增。"""
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            revision = repository.get_revision_shared(cur)
    return RevisionOut(revision=revision)


# ---------------------------------------------------------------- 批量原子变更

@app.post(
    "/api/changes",
    response_model=BatchChangeResponse,
    tags=["batch"],
    responses={
        409: {"description": "expected_revision 已过期（revision_conflict）"},
        422: {"description": "区域不存在/重复操作/非法数据/轨迹不安全等"},
    },
)
def apply_batch_change(payload: BatchChangeRequest):
    """把多处限行区域变更作为一次原子提交，并保证指定轨迹变更后仍安全。

    流程（全部在单个写事务内，任何一步失败均整体回滚、不落库）：
    1. 静态校验：同一现有区域在一批中只能出现一次；update 必须至少带一个字段；
       create/合并后的生效时段必须满足 valid_from < valid_to；
    2. 锁定 region_meta 单行，读取当前 revision 并与 expected_revision 比较，
       过期则 409（revision_conflict）；
    3. 按 id 升序锁定本批涉及的现存区域（FOR UPDATE），不存在则 422
       （region_not_found）；构造变更后的完整区域集合；
    4. 对每条带时区的绝对时刻轨迹，按直线匀速、边界接触即穿越、生效时段
       左闭右开规则核验「变更后」完整集合；任一轨迹不安全则 422
       （trajectory_unsafe，列出轨迹与逐区域穿越明细），不落库；
    5. 全部合法且安全后，统一执行登记/修订/删除：修订沿用 version+1，整批
       全局 revision 只递增一次，随后提交。

    并发性：meta 单行锁串行化所有写事务，区域行锁避免丢失更新；并发区域变更
    无法绕过第 4 步核验（行锁会等待并发写者提交，随后按其已提交结果重新核验）。
    """
    # ---- 1. 不依赖数据库的静态校验（快速失败，此时尚未开启写事务） ----
    seen: dict[int, int] = {}
    for index, entry in enumerate(payload.operations):
        if entry.op in ("update", "delete"):
            rid = entry.region_id
            if rid in seen:
                raise DuplicateRegionInBatch(rid, index)
            seen[rid] = index
        if entry.op == "update":
            body = entry.region
            if (body.name is None and body.polygon is None
                    and body.valid_from is None and body.valid_to is None):
                raise DomainError(
                    "update 操作必须至少给出一个待修订字段（name/polygon/"
                    "valid_from/valid_to）",
                    ["body", "operations", index, "region"],
                )

    # 轨迹在几何层统一校验时刻严格递增、坐标有限等（带时区已由 schema 保证）
    trajectories = [
        geometry.validate_trajectory(spec.points, lambda p: p.t)
        for spec in payload.trajectories
    ]

    # ---- 2-5. 单事务内完成 revision 检查、加锁、核验与落库 ----
    with db.pool.connection() as conn:
        conn.rollback()  # 丢弃连接池中可能残留的事务状态
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            cur.execute("BEGIN")
            try:
                # meta 行锁：拿到稳定的当前 revision，并串行化所有批量写
                cur.execute(
                    "SELECT revision FROM region_meta WHERE id = 1 FOR UPDATE"
                )
                current_rev = cur.fetchone()["revision"]
                if current_rev != payload.expected_revision:
                    raise RevisionConflict(payload.expected_revision, current_rev)

                # 锁定全部现存区域（按 id 升序 FOR UPDATE）：核验针对的是变更后
                # 完整集合，锁定所有行可避免读到并发写者的中间状态，同时让本事务
                # 等待并发写者提交后基于其最新已提交结果构造集合
                locked = repository.lock_all_region_rows(cur)
                try:
                    proposed, deleted_versions = (
                        repository.build_proposed_regions(
                            payload.operations, locked
                        )
                    )
                except repository.RegionMissing as exc:
                    raise RegionNotFoundInBatch(exc.region_id, exc.op_index) from exc

                # 对变更后的完整集合核验全部轨迹
                all_checks = []
                unsafe_details = []
                for i, traj in enumerate(trajectories):
                    results = geometry.verify_trajectory(traj, proposed)
                    safe = all(len(r["crossings"]) == 0 for r in results)
                    all_checks.append({
                        "traj_index": i,
                        "name": payload.trajectories[i].name,
                        "safe": safe,
                        "results": results,
                    })
                    if not safe:
                        violations = [
                            {
                                "region_id": r["region_id"],
                                "region_name": r["region_name"],
                                "crossings": r["crossings"],
                            }
                            for r in results if r["crossings"]
                        ]
                        unsafe_details.append({
                            "loc": ["body", "trajectories", i],
                            "traj_index": i,
                            "name": payload.trajectories[i].name,
                            "violations": violations,
                        })
                if unsafe_details:
                    # 穿越时刻此时是 epoch 秒，转成 UTC ISO8601（带 Z）便于客户端阅读
                    for detail in unsafe_details:
                        for v in detail["violations"]:
                            v["crossings"] = [
                                {
                                    **c,
                                    "entry_time": _iso_z(datetime.fromtimestamp(
                                        c["entry_time"], tz=timezone.utc)),
                                    "exit_time": _iso_z(datetime.fromtimestamp(
                                        c["exit_time"], tz=timezone.utc)),
                                }
                                for c in v["crossings"]
                            ]
                    raise TrajectoryUnsafe(unsafe_details)

                # 全部安全：统一落库，整批 revision 只 +1
                affected, new_revision = repository.apply_changes(
                    cur, payload.operations, locked
                )
            except (RevisionConflict, RegionNotFoundInBatch,
                    DuplicateRegionInBatch, TrajectoryUnsafe,
                    repository.PlanValidationError, DomainError,
                    geometry.TrajectoryError, geometry.RegionError):
                conn.rollback()
                raise
            except (DeadlockDetected, SerializationFailure) as exc:
                conn.rollback()
                raise DomainError(
                    "区域并发修改冲突，请重新读取最新 revision 与区域集合后重试"
                ) from exc

        # with-cursor 已结束但事务仍打开；显式提交
        conn.commit()

    # ---- 组装成功响应（事务外，仅做数据整形） ----
    affected_by_index = {a["op_index"]: a["region"] for a in affected}
    # 新建区域核验时用的是负临时 id，这里按 op_index 回填为真实 id
    temp_to_real: dict[int, int] = {}
    create_seq = 0
    for index, entry in enumerate(payload.operations):
        if entry.op == "create":
            create_seq += 1
            temp_to_real[-create_seq] = affected_by_index[index]["id"]
    op_results = []
    for index, entry in enumerate(payload.operations):
        if entry.op == "create":
            region = affected_by_index[index]
            op_results.append({
                "op": "create",
                "op_index": index,
                "region_id": region["id"],
                "region": RegionOut.model_validate(region),
                "version": region["version"],
            })
        elif entry.op == "update":
            region = affected_by_index[index]
            op_results.append({
                "op": "update",
                "op_index": index,
                "region_id": entry.region_id,
                "region": RegionOut.model_validate(region),
                "version": region["version"],
            })
        else:
            deleted_row = next(
                d for d in deleted_versions if d["region_id"] == entry.region_id
            )
            op_results.append({
                "op": "delete",
                "op_index": index,
                "region_id": entry.region_id,
                "version": deleted_row["version"],
            })

    traj_out = []
    for chk in all_checks:
        shaped = _crossings_out(chk["results"])
        for r in shaped:
            if r["region_id"] in temp_to_real:
                r["region_id"] = temp_to_real[r["region_id"]]
        traj_out.append(TrajectoryCheckOut(
            traj_index=chk["traj_index"],
            name=chk["name"],
            safe=True,
            results=[RegionResultOut.model_validate(r) for r in shaped],
        ))

    return BatchChangeResponse(
        revision=new_revision,
        operations=op_results,
        trajectories=traj_out,
    )


# ---------------------------------------------------------------- 轨迹核验

def _crossings_out(results) -> list[dict]:
    """把几何引擎的 epoch 秒穿越结果转成响应用的 UTC datetime 结构。"""
    out_results = []
    for r in results:
        crossings = []
        for c in r["crossings"]:
            crossings.append({
                "entry_time": datetime.fromtimestamp(c["entry_time"], tz=timezone.utc),
                "exit_time": datetime.fromtimestamp(c["exit_time"], tz=timezone.utc),
                "entry_inclusive": c["entry_inclusive"],
                "exit_inclusive": c["exit_inclusive"],
                "point_contact": c["point_contact"],
            })
        out_results.append({
            "region_id": r["region_id"],
            "region_name": r["region_name"],
            "crossings": crossings,
        })
    return out_results


@app.post("/api/verify", response_model=VerifyResponse, tags=["verification"])
def verify(payload: VerifyRequest):
    """对整段连续轨迹核验。

    整个核验在一个 REPEATABLE READ 只读事务中完成，因此并发修改区域时，
    本次核验使用的是同一已提交版本的区域集合（响应中带 revision）。
    """
    # 时刻严格递增在几何层统一报错；先快速失败避免无谓的数据库读取
    traj = geometry.validate_trajectory(payload.points, lambda p: p.t)

    with db.pool.connection() as conn:
        # REPEATABLE READ 只读事务：事务内所有读取共享同一快照，
        # 即使核验期间有区域被并发提交修改，本请求仍使用同一已提交版本的集合
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            repository.configure_cursor(cur)
            try:
                revision, regions = repository.load_snapshot(cur)
                # 几何计算在事务内完成：快照一直持有到本次核验结束
                results = geometry.verify_trajectory(traj, regions)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    checked_at = datetime.now(timezone.utc)
    safe = all(len(r["crossings"]) == 0 for r in results)
    out_results = _crossings_out(results)

    return VerifyResponse(
        safe=safe,
        revision=revision,
        checked_at=checked_at,
        results=out_results,
    )


@app.post("/api/schedule", response_model=ScheduleResponse, tags=["scheduling"])
def schedule(payload: ScheduleRequest):
    """计算固定行程在候选出发时段内的全部安全出发区间。

    轨迹点用相对首点的秒数给出（首点必须为 0、其余严格递增）；候选时段
    [window_start, window_end) 带时区，服务端归一化到 UTC。计算为精确的
    区间运算（不做离散抽样）：对每个区域、每段几何接触 [a,b]（闭）与生效
    窗口 [vf, vt)（左闭右开），其禁止出发时刻恰为半开区间 [vf-b, vt-a)；
    全部禁止区间取并集后从候选窗口中扣除。

    整个计算在一个 REPEATABLE READ 只读事务中完成，与 /api/verify 一样，
    并发修改区域时本请求使用同一已提交快照（响应中带 revision）。
    """
    traj = geometry.validate_relative_trajectory(
        payload.points, lambda p: (p.x, p.y), lambda p: p.t
    )
    if payload.window_start >= payload.window_end:
        raise DomainError(
            "候选时段必须满足 window_start < window_end（左闭右开，起早于止）",
            ["window_start", "window_end"],
        )

    window_start = payload.window_start.timestamp()
    window_end = payload.window_end.timestamp()

    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            repository.configure_cursor(cur)
            try:
                revision, regions = repository.load_snapshot(cur)
                # 几何计算在事务内完成：快照一直持有到本次计算结束
                intervals = geometry.safe_departure_intervals(
                    traj, regions, window_start, window_end
                )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    out_intervals = [
        {
            "start": datetime.fromtimestamp(iv["start"], tz=timezone.utc),
            "end": datetime.fromtimestamp(iv["end"], tz=timezone.utc),
            "start_inclusive": iv["start_inclusive"],
            "end_inclusive": iv["end_inclusive"],
        }
        for iv in intervals
    ]

    return ScheduleResponse(
        revision=revision,
        window_start=payload.window_start,
        window_end=payload.window_end,
        intervals=out_intervals,
    )


@app.get("/health", tags=["meta"])
def health():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "临时限行核验服务",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": [
            "POST /api/regions", "GET /api/regions", "GET /api/regions/{id}",
            "PATCH /api/regions/{id}", "DELETE /api/regions/{id}",
            "GET /api/revision", "POST /api/changes",
            "POST /api/verify", "POST /api/schedule",
            "GET /health",
        ],
    }
