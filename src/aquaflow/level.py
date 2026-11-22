"""수위 — K-water 실시간 수도정보 수위(시간) (data.go.kr 15057371, B500001/rwis/waterLevel/list).

정수장 정수지 · **배수지** 수위(m)를 시간마다. 한 시설에 탱크가 여럿이라(배수지#1 · #2 …) 시계열 단위는
**태그(dataItemTagsn)** 다. 수질과 같은 키(KWATER_API_KEY) — 다만 data.go.kr 에서 이 서비스도 활용 신청해야 한다.

2026-11-22 실제 키로 확인:
  - 경로 `/list`. 날짜는 명세의 `20261122` 가 아니라 `2026-11-22` (명세대로 보내면 60초 뒤 504 SERVICETIMEOUT)
  - 매우 느리다: 5행에 약 37초
  - 응답: occrrncDt(숫자 YYYYMMDDHH) · fcltyNm · fcltyMngNo · dataItemDesc(탱크 이름) · dataItemTagsn · dataVal(m) · itemUnit · dataItemDiv(M = 정시 순간값)
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timedelta

import pyarrow as pa

from . import ingest, store

SCHEMA = pa.schema([
    ("ts", pa.timestamp("s")),
    ("facility_name", pa.string()),
    ("facility_code", pa.string()),
    ("tag", pa.string()),
    ("tank", pa.string()),
    ("level_m", pa.float64()),
    ("unit", pa.string()),
    ("kind", pa.string()),
    ("source", pa.string()),
    ("flags", pa.string()),
])

PHYSICAL_M = (0.0, 100.0)        # 이 밖이면 격리 (센서 고장 · 조작 · 단위 오류)
MAX_HOURLY_JUMP_M = 1.5          # 한 시간에 이보다 크게 바뀌면 표시 — 출발점. 실제 탱크별 변동을 보고 고친다


def key(r: dict) -> tuple:
    return (r.get("tag") or "", r["ts"])


def kind_of(name: str, desc: str) -> str:
    text = f"{name} {desc}"
    return "배수지" if "배수지" in text else "정수지" if ("정수지" in text or "정수장" in text) else ""


def standard(it: dict) -> dict:
    name, desc = ingest._text(it.get("fcltyNm")), ingest._text(it.get("dataItemDesc"))
    return {
        "ts": ingest.parse_ts(it.get("occrrncDt")),
        "facility_name": name,
        "facility_code": ingest._text(it.get("fcltyMngNo")),
        "tag": ingest._text(it.get("dataItemTagsn")),
        "tank": desc,
        "level_m": ingest.to_float(it.get("dataVal")),
        "unit": ingest._text(it.get("itemUnit")),
        "kind": kind_of(name, desc),
        "source": "api",
    }


def fetch(st_date: str, ed_date: str, st_hour: str, ed_hour: str, rows: int = 100) -> list[dict]:
    url = os.environ.get("KWATER_LEVEL_URL", "https://apis.data.go.kr/B500001/rwis/waterLevel/list")
    key_ = os.environ.get("KWATER_API_KEY")
    if not key_:
        raise RuntimeError("KWATER_API_KEY 가 없음")
    out, page = [], 1
    while True:
        params = {"serviceKey": key_, "stDt": st_date, "stTm": st_hour, "edDt": ed_date, "edTm": ed_hour,
                  "numOfRows": rows, "pageNo": page, "_type": "json"}
        body = ingest._get(url, params, key_)
        if not body.strip():                                 # 수질 API 와 같은 빈 응답 — 쪽 크기를 반으로
            if rows <= 10:
                raise RuntimeError("수위 API 가 빈 응답만 준다 (쪽 크기 10 에서도)")
            rows //= 2
            out, page = [], 1
            continue
        items = ingest._items_from_response(body)
        out += [standard(it) for it in items]
        if len(items) < rows:
            return out
        page += 1
        if page > ingest.MAX_PAGES:
            raise RuntimeError("수위 API 쪽이 너무 많음 — 기간을 줄인다")


def check(rows: list[dict], now: datetime) -> tuple[list[dict], list[dict], dict]:
    """중복: 값까지 같으면 조용히 하나만 (API 쪽 넘김 중에 새 행이 앞에 붙어 같은 행이 다시 온다 — 2026-11-22 실측 22행),
    값이 다르면 격리 (같은 탱크 · 시각에 두 값 = 의심)."""
    kept, rejected, seen, reasons = [], [], {}, Counter()
    identical = 0
    for r in rows:
        why = []
        if r["ts"] is None:
            why.append("ts_unparsable")
        elif r["ts"] > now + timedelta(hours=2):
            why.append("ts_future")
        if not r["tag"]:
            why.append("tag_missing")
        v = r["level_m"]
        if v is None:
            why.append("value_missing")
        elif not (PHYSICAL_M[0] <= v <= PHYSICAL_M[1]):
            why.append("level_impossible")
        if r["unit"] and r["unit"].lower() != "m":
            why.append("unit_not_m")
        if not why and key(r) in seen:
            if seen[key(r)] == r["level_m"]:
                identical += 1
                continue
            why.append("duplicate_conflict")
        if why:
            reasons.update(why)
            rejected.append({**r, "reject_reasons": ",".join(why)})
            continue
        seen[key(r)] = r["level_m"]
        kept.append({**r, "flags": []})
    kept.sort(key=key)
    prev = None
    for r in kept:
        if prev and prev["tag"] == r["tag"] and r["ts"] - prev["ts"] == timedelta(hours=1) \
                and abs(r["level_m"] - prev["level_m"]) > MAX_HOURLY_JUMP_M:
            r["flags"].append("level_jump")
        prev = r
    for r in kept:
        r["flags"] = ",".join(r["flags"])
    report = {"input_rows": len(rows), "kept_rows": len(kept), "rejected_rows": len(rejected), "dedup_identical": identical,
              "reject_reasons": dict(reasons), "flags": dict(Counter(r["flags"] for r in kept if r["flags"])),
              "facilities": len({r["facility_code"] for r in kept}), "tanks": len({r["tag"] for r in kept}),
              "kinds": dict(Counter(r["kind"] or "?" for r in kept))}
    return kept, rejected, report


def write(kept: list[dict], out_dir):
    return store.write_orc(kept, out_dir, schema=SCHEMA, subdir="curated_level", key=key)
