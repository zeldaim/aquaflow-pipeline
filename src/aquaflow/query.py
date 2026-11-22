"""조회 — ORC 를 DuckDB 로.

DuckDB 는 ORC 를 직접 읽지 못한다 (Parquet · CSV · JSON 만 기본). 그래서 pyarrow 로 읽은 Arrow 표를
DuckDB 에 넘겨 SQL 을 돌린다 — 복사 없이 같은 메모리를 본다.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.orc as orc

SUMMARY_SQL = """
SELECT plant_name,
       count(*)                          AS hours,
       min(ts)                           AS first_ts,
       max(ts)                           AS last_ts,
       round(avg(ph), 2)                 AS ph_avg,
       round(avg(turbidity_ntu), 3)      AS turbidity_avg,
       round(avg(chlorine_mgl), 3)       AS chlorine_avg,
       count(*) FILTER (WHERE flags <> '') AS flagged_hours
FROM wq
GROUP BY plant_name
ORDER BY flagged_hours DESC, plant_name
"""


TABLES = {"wq": "curated", "lv": "curated_level", "wx": "curated_weather"}   # 수질 · 수위 · 강수량


def load(out_dir: str | Path, subdir: str = "curated") -> pa.Table | None:
    files = sorted((Path(out_dir) / subdir).glob("date=*/part-0.orc"))
    if not files:
        return None
    return pa.concat_tables([orc.read_table(str(f)) for f in files])


def run(out_dir: str | Path, sql: str = SUMMARY_SQL):
    """테이블: wq(수질) · lv(수위) · wx(강수량) — 있는 것만 등록. 예) 표시된 수질 시각의 그 지역 강수량 JOIN"""
    con = duckdb.connect()
    for name, sub in TABLES.items():
        t = load(out_dir, sub)
        if t is not None:
            con.register(name, t)
    if "wq" not in {r[0] for r in con.execute("SHOW TABLES").fetchall()}:
        raise FileNotFoundError(f"{out_dir}/curated 에 ORC 가 없음 — 먼저 ingest")
    return con.execute(sql).fetchall(), [d[0] for d in con.description]
