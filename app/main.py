"""FastAPI 入口：临时限行核验 HTTP API。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.errors import CheckViolation
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, geometry, repository
from .schemas import (
    RegionCreate,
    RegionCreatedResponse,
    RegionOut,
    RegionUpdate,
    RevisionOut,
    ScheduleRequest,
    ScheduleResponse,
    VerifyRequest,
    VerifyResponse,
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
    version="1.1.0",
    description="登记平面多边形限行区域、对连续轨迹进行穿越核验，并计算固定行程的安全出发区间。",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 错误处理

class DomainError(Exception):
    """业务语义错误（HTTP 422）。"""

    def __init__(self, message: str, loc: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.loc = loc or []


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


# ---------------------------------------------------------------- 轨迹核验

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
            "GET /api/revision", "POST /api/verify", "POST /api/schedule",
            "GET /health",
        ],
    }
