"""품질 검증 — 저장하기 전에 행을 셋으로 나눈다.

  rejected (격리)  물리적으로 불가능하거나 깨진 값 — 저장도 학습도 안 함.
                   센서 조작 · 전송 오류 · 학습 데이터 오염(poisoning)을 막는 첫 관문.
  flagged  (표시)  값은 가능하지만 기준을 넘음 — 저장은 하고 표시만 (실제 사건일 수 있으니 버리지 않음)
  ok

기준값은 THRESHOLDS 한 곳에 모았다. 수질 기준치(먹는물 수질기준 등)는 법령 최신판으로 확인하고 고친다 —
여기 값은 연습용 출발점이다.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

# 물리적으로 가능한 범위 — 밖이면 격리
PHYSICAL = {
    "ph": (0.0, 14.0),
    "turbidity_ntu": (0.0, 1000.0),
    "chlorine_mgl": (0.0, 20.0),
}

# 기준 — 벗어나면 표시. K-water 내려받은 파일(2026-11-22)의 기준치 줄과 같게: pH 5.8~8.5 · 탁도 0.5 이하 · 잔류염소 0.1~4.0
# 잔류염소 하한(0.1)이 중요하다 — 너무 낮으면 소독이 안 된 물이 나간다는 뜻
THRESHOLDS = {
    "ph": (5.8, 8.5),
    "turbidity_ntu": (None, 0.5),
    "chlorine_mgl": (0.1, 4.0),
}

# 한 시간 사이에 이보다 크게 바뀌면 표시 (정수장 출구 수질은 보통 천천히 변한다)
MAX_HOURLY_JUMP = {"ph": 1.0, "turbidity_ntu": 0.3, "chlorine_mgl": 0.5}

FUTURE_SLACK = timedelta(hours=2)


def series_key(r: dict) -> tuple:
    """한 시계열 = 시설 × 용수 구분. 한 정수장이 생활정수 · 공업용수를 같은 시설관리번호로 따로 낸다
    (2026-11-22 실제 API: 시흥 · 청주 · 천안 · 아산 …) — 구분을 빼면 한쪽을 '중복'으로 버리게 된다."""
    return (r["plant_code"] or r["plant_name"], r["use_kind"])


def is_drinking(r: dict) -> bool:
    """먹는물 기준(THRESHOLDS)은 생활용수에만. 공업용수는 먹는물이 아니라 기준이 다르다 — 물리 범위 · 급변만 본다.
    구분이 비어 있으면(예전 파일) 생활로 본다."""
    kind = r["use_kind"]
    return not kind or kind.startswith("생활")


def check(rows: list[dict], now: datetime | None = None) -> tuple[list[dict], list[dict], dict]:
    """(통과한 행 · 격리된 행 · 보고서). 통과한 행에는 flags(쉼표 문자열)가 붙는다."""
    if now is None:                                         # 데이터가 한국 시간이라 기준도 한국 시간 (컨테이너는 UTC)
        from .ingest import now_kst
        now = now_kst()
    kept, rejected = [], []
    seen = {}                       # 키 → 값. 값까지 같은 중복은 조용히 하나만 (API 넘김 겹침), 다르면 격리
    identical = 0
    reasons = Counter()
    flags_count = Counter()

    for r in rows:
        why = []
        if r["ts"] is None:
            why.append("ts_unparsable")
        elif r["ts"] > now + FUTURE_SLACK:
            why.append("ts_future")
        if not r["plant_name"]:
            why.append("plant_missing")
        values = [r[k] for k in PHYSICAL]
        if all(v is None for v in values):
            why.append("all_values_missing")
        for k, (lo, hi) in PHYSICAL.items():
            v = r[k]
            if v is not None and not (lo <= v <= hi):
                why.append(f"{k}_impossible")
        key = (*series_key(r), r["ts"])
        vals = tuple(r[k] for k in PHYSICAL)
        if not why and key in seen:
            if seen[key] == vals:
                identical += 1
                continue
            why.append("duplicate_conflict")
        if why:
            reasons.update(why)
            rejected.append({**r, "reject_reasons": ",".join(why)})
            continue
        seen[key] = vals

        flags = []
        for k, (lo, hi) in (THRESHOLDS.items() if is_drinking(r) else ()):
            v = r[k]
            if v is None:
                continue
            if hi is not None and v > hi:
                flags.append(f"{k}_over_standard")
            elif lo is not None and v < lo:
                flags.append(f"{k}_under_standard")
        kept.append({**r, "flags": flags})

    # 시간당 급변 — 같은 시설의 이웃한 시각끼리 비교
    kept.sort(key=lambda r: (*series_key(r), r["ts"]))
    prev = None
    for r in kept:
        same = prev is not None and series_key(prev) == series_key(r)
        if same and r["ts"] - prev["ts"] == timedelta(hours=1):
            for k, limit in MAX_HOURLY_JUMP.items():
                if r[k] is not None and prev[k] is not None and abs(r[k] - prev[k]) > limit:
                    r["flags"].append(f"{k}_jump")
        prev = r
    for r in kept:
        flags_count.update(r["flags"])
        r["flags"] = ",".join(r["flags"])

    report = {
        "input_rows": len(rows),
        "kept_rows": len(kept),
        "rejected_rows": len(rejected),
        "dedup_identical": identical,
        "reject_reasons": dict(reasons),
        "flags": dict(flags_count),
        "plants": len({r["plant_code"] or r["plant_name"] for r in kept}),
        "series": len({series_key(r) for r in kept}),          # 시설 × 용수 구분
        "ts_min": min((r["ts"] for r in kept), default=None),
        "ts_max": max((r["ts"] for r in kept), default=None),
    }
    return kept, rejected, report
