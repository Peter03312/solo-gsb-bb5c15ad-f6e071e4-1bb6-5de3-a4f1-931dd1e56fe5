"""区域数据访问：CRUD、一致性快照读取与批量原子变更。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from psycopg.rows import dict_row
from shapely.geometry import Polygon
from shapely.wkt import dumps as wkt_dumps

from . import geometry


class PlanValidationError(ValueError):
    """批量变更计划本身非法（如修订后 valid_from >= valid_to）。"""


def _epoch(dt: datetime) -> float:
    return dt.timestamp()


def _row_to_region(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "polygon": row["polygon"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_revision(cur) -> int:
    cur.execute("SELECT revision FROM region_meta WHERE id = 1 FOR UPDATE")
    return cur.fetchone()["revision"]


def get_revision_shared(cur) -> int:
    cur.execute("SELECT revision FROM region_meta WHERE id = 1")
    return cur.fetchone()["revision"]


def bump_revision(cur) -> int:
    """在写事务内推进全局版本号（调用方应已持有该行行锁）。"""
    cur.execute(
        "UPDATE region_meta SET revision = revision + 1 WHERE id = 1 "
        "RETURNING revision"
    )
    return cur.fetchone()["revision"]


def _insert_region(cur, name, valid_from, valid_to, polygon_vertices) -> dict:
    wkt = wkt_dumps(Polygon(polygon_vertices), rounding_precision=-1)
    cur.execute(
        """
        INSERT INTO regions (name, valid_from, valid_to, polygon, wkt)
        VALUES (%(name)s, %(valid_from)s, %(valid_to)s, %(polygon)s::jsonb, %(wkt)s)
        RETURNING id, name, polygon, valid_from, valid_to, version,
                  created_at, updated_at
        """,
        {
            "name": name,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "polygon": json.dumps(polygon_vertices),
            "wkt": wkt,
        },
    )
    return _row_to_region(cur.fetchone())


def _update_region_locked(cur, row: dict, name, valid_from, valid_to,
                          polygon_json, wkt) -> dict:
    cur.execute(
        """
        UPDATE regions SET
            name = %(name)s,
            valid_from = %(valid_from)s,
            valid_to = %(valid_to)s,
            polygon = %(polygon)s::jsonb,
            wkt = %(wkt)s,
            version = version + 1,
            updated_at = now()
        WHERE id = %(id)s
        RETURNING id, name, polygon, valid_from, valid_to, version,
                  created_at, updated_at
        """,
        {
            "id": row["id"],
            "name": name,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "polygon": json.dumps(polygon_json),
            "wkt": wkt,
        },
    )
    return _row_to_region(cur.fetchone())


def create_region(cur, payload, polygon_vertices: list[list[float]]) -> tuple[dict, int]:
    # 锁定元数据行，使区域写入串行化并拿到稳定的新版本号
    get_revision(cur)
    row = _insert_region(
        cur, payload.name, payload.valid_from, payload.valid_to, polygon_vertices
    )
    revision = bump_revision(cur)
    return row, revision


def list_regions(cur) -> list[dict]:
    cur.execute(
        """
        SELECT id, name, polygon, valid_from, valid_to, version,
               created_at, updated_at
        FROM regions
        ORDER BY id
        """
    )
    return [_row_to_region(r) for r in cur.fetchall()]


def get_region_for_update(cur, region_id: int) -> dict | None:
    cur.execute(
        """
        SELECT id, name, polygon, valid_from, valid_to, version,
               created_at, updated_at, wkt
        FROM regions WHERE id = %s
        FOR UPDATE
        """,
        (region_id,),
    )
    return cur.fetchone()


def update_region(cur, region_id: int, payload, new_polygon) -> tuple[dict, int] | None:
    row = get_region_for_update(cur, region_id)
    if row is None:
        return None

    name = payload.name if payload.name is not None else row["name"]
    valid_from = payload.valid_from if payload.valid_from is not None else row["valid_from"]
    valid_to = payload.valid_to if payload.valid_to is not None else row["valid_to"]
    if valid_from >= valid_to:
        raise ValueError("生效时段必须满足 valid_from < valid_to（左闭右开，起早于止）")

    if new_polygon is not None:
        polygon_json = new_polygon
        wkt = wkt_dumps(Polygon(new_polygon), rounding_precision=-1)
    else:
        polygon_json = row["polygon"]
        wkt = row["wkt"]

    updated = _update_region_locked(cur, row, name, valid_from, valid_to,
                                    polygon_json, wkt)
    get_revision(cur)
    revision = bump_revision(cur)
    return updated, revision


def delete_region(cur, region_id: int) -> tuple[bool, int | None]:
    cur.execute("DELETE FROM regions WHERE id = %s RETURNING id", (region_id,))
    deleted = cur.fetchone() is not None
    revision = None
    if deleted:
        get_revision(cur)
        revision = bump_revision(cur)
    return deleted, revision


# ---------------------------------------------------------------- 批量原子变更

class RegionMissing(Exception):
    """批量变更引用了不存在的区域。"""

    def __init__(self, region_id: int, op_index: int):
        super().__init__(f"区域 {region_id} 不存在")
        self.region_id = region_id
        self.op_index = op_index


def lock_all_region_rows(cur) -> dict[int, dict]:
    """按 id 升序锁定全部现存区域（FOR UPDATE）。

    批量核验针对的是「变更后完整集合」，因此不仅本批直接改/删的行，集合里
    其它现存区域也必须锁定：READ COMMITTED 下普通快照读可能在语句间看到并发
    写者的中间结果，全量行锁让构造集合、几何核验与落库基于同一份已提交数据。
    """
    cur.execute(
        """
        SELECT id, name, polygon, valid_from, valid_to, version,
               created_at, updated_at, wkt
        FROM regions
        ORDER BY id
        FOR UPDATE
        """
    )
    return {row["id"]: row for row in cur.fetchall()}


def build_proposed_regions(operations, locked: dict[int, dict]) -> tuple[list[dict], list[dict]]:
    """在已持有 meta 行锁与全部区域行锁后，构造「变更后」的完整区域集合。

    operations: 已通过 endpoint 层静态校验的操作对象列表。
    locked: lock_all_region_rows 得到的 id -> 现存行（含 wkt），即当前已提交集合。

    返回 (proposed_regions, deleted_versions)：
    * proposed_regions 供几何核验：id 为现存真实 id 或新建区的负临时 id
      （落库后按 _op_index 回填真实 id），另带 _op_index；
    * deleted_versions: [{"region_id", "version"}] 删除前的版本号。

    区域不存在抛 RegionMissing（由 main 层处理）；合并后时段非法抛
    PlanValidationError。
    """
    proposed: list[dict] = []
    deleted: set[int] = set()
    deleted_versions: list[dict] = []

    # 先收集各类型操作并记录其在请求中的下标；集合的最终形态与操作顺序无关。
    updates: dict[int, tuple[int, object]] = {}
    creates: list[tuple[int, object]] = []
    for index, entry in enumerate(operations):
        kind = entry.op
        if kind == "delete":
            row = locked.get(entry.region_id)
            if row is None:
                raise RegionMissing(entry.region_id, index)
            deleted.add(entry.region_id)
            deleted_versions.append({"region_id": entry.region_id,
                                     "version": row["version"]})
        elif kind == "update":
            if entry.region_id not in locked:
                raise RegionMissing(entry.region_id, index)
            updates[entry.region_id] = (index, entry)
        else:
            creates.append((index, entry))

    # 现存区域（未被删除的）应用部分更新；全部来自已锁定行，不再二次读库
    for rid in sorted(locked):
        locked_row = locked[rid]
        if rid in deleted:
            continue
        update = updates.get(rid)
        if update is None:
            proposed.append({
                "id": rid,
                "name": locked_row["name"],
                "valid_from": _epoch(locked_row["valid_from"]),
                "valid_to": _epoch(locked_row["valid_to"]),
                "polygon": geometry.polygon_from_wkt(locked_row["wkt"]),
                "_op_index": None,
            })
            continue

        op_index, entry = update
        payload = entry.region
        name = payload.name if payload.name is not None else locked_row["name"]
        valid_from = (payload.valid_from if payload.valid_from is not None
                      else locked_row["valid_from"])
        valid_to = (payload.valid_to if payload.valid_to is not None
                    else locked_row["valid_to"])
        if valid_from >= valid_to:
            raise PlanValidationError(
                "修订后生效时段必须满足 valid_from < valid_to（左闭右开，起早于止）"
            )
        if payload.polygon is not None:
            polygon = Polygon(payload.polygon)
        else:
            polygon = geometry.polygon_from_wkt(locked_row["wkt"])
        proposed.append({
            "id": rid,
            "name": name,
            "valid_from": _epoch(valid_from),
            "valid_to": _epoch(valid_to),
            "polygon": polygon,
            "_op_index": op_index,
        })

    # 新建区域：使用负临时 id，真正 id 在 apply_changes 后按 op_index 回填
    for seq, (index, entry) in enumerate(creates, start=1):
        payload = entry.region
        if payload.valid_from >= payload.valid_to:
            raise PlanValidationError(
                "生效时段必须满足 valid_from < valid_to（左闭右开，起早于止）"
            )
        proposed.append({
            "id": -seq,
            "name": payload.name,
            "valid_from": _epoch(payload.valid_from),
            "valid_to": _epoch(payload.valid_to),
            "polygon": Polygon(payload.polygon),
            "_op_index": index,
        })

    return proposed, deleted_versions


def apply_changes(cur, operations, locked: dict[int, dict]) -> tuple[list[dict], int]:
    """在已完成校验（含轨迹安全）的同一事务内落库全部操作，revision 只 +1。

    调用前必须已对 region_meta 加行锁并确认 revision 匹配，且已对全部涉及的
    现存区域加行锁（FOR UPDATE，即 locked）。返回 (affected_regions, new_revision)：
    affected_regions 为 {"op_index", "region"}（新建 region.id 为真实新 id），
    删除操作不包含其中。
    """
    affected: list[dict] = []
    for index, entry in enumerate(operations):
        kind = entry.op
        if kind == "create":
            p = entry.region
            row = _insert_region(cur, p.name, p.valid_from, p.valid_to, p.polygon)
            affected.append({"op_index": index, "region": row})
        elif kind == "update":
            # 复用事务前段已锁定的行（FOR UPDATE），不再二次查询
            row = locked[entry.region_id]
            p = entry.region
            name = p.name if p.name is not None else row["name"]
            valid_from = p.valid_from if p.valid_from is not None else row["valid_from"]
            valid_to = p.valid_to if p.valid_to is not None else row["valid_to"]
            if p.polygon is not None:
                polygon_json = p.polygon
                wkt = wkt_dumps(Polygon(p.polygon), rounding_precision=-1)
            else:
                polygon_json = row["polygon"]
                wkt = row["wkt"]
            updated = _update_region_locked(
                cur, row, name, valid_from, valid_to, polygon_json, wkt
            )
            affected.append({"op_index": index, "region": updated})
        else:  # delete
            cur.execute(
                "DELETE FROM regions WHERE id = %s RETURNING id",
                (entry.region_id,),
            )
            cur.fetchone()
    new_revision = bump_revision(cur)
    return affected, new_revision


def load_snapshot(cur) -> tuple[int, list[dict]]:
    """在 REPEATABLE READ 只读事务中读取同一已提交版本的区域集合。

    返回 (revision, regions)；regions 内已构造好 Shapely Polygon。
    """
    revision = get_revision_shared(cur)
    cur.execute(
        """
        SELECT id, name, valid_from, valid_to, wkt
        FROM regions
        ORDER BY id
        """
    )
    rows = cur.fetchall()
    regions = []
    for row in rows:
        regions.append({
            "id": row["id"],
            "name": row["name"],
            "valid_from": _epoch(row["valid_from"]),
            "valid_to": _epoch(row["valid_to"]),
            "polygon": geometry.polygon_from_wkt(row["wkt"]),
        })
    return revision, regions


def configure_cursor(cur) -> None:
    cur.row_factory = dict_row


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
