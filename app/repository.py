"""区域数据访问：CRUD 与一致性快照读取。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from psycopg.rows import dict_row
from shapely.wkt import dumps as wkt_dumps

from . import geometry


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


def create_region(cur, payload, polygon_vertices: list[list[float]]) -> tuple[dict, int]:
    from shapely.geometry import Polygon

    wkt = wkt_dumps(Polygon(polygon_vertices), rounding_precision=-1)
    # 锁定元数据行，使区域写入串行化并拿到稳定的新版本号
    get_revision(cur)
    cur.execute(
        """
        INSERT INTO regions (name, valid_from, valid_to, polygon, wkt)
        VALUES (%(name)s, %(valid_from)s, %(valid_to)s, %(polygon)s::jsonb, %(wkt)s)
        RETURNING id, name, polygon, valid_from, valid_to, version,
                  created_at, updated_at
        """,
        {
            "name": payload.name,
            "valid_from": payload.valid_from,
            "valid_to": payload.valid_to,
            "polygon": json.dumps(polygon_vertices),
            "wkt": wkt,
        },
    )
    row = cur.fetchone()
    revision = bump_revision(cur)
    return _row_to_region(row), revision


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
        from shapely.geometry import Polygon

        polygon_json = new_polygon
        wkt = wkt_dumps(Polygon(new_polygon), rounding_precision=-1)
    else:
        polygon_json = row["polygon"]
        wkt = row["wkt"]

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
            "id": region_id,
            "name": name,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "polygon": json.dumps(polygon_json),
            "wkt": wkt,
        },
    )
    updated = _row_to_region(cur.fetchone())
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
