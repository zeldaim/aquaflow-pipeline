"""강수량 — 기상청 지상(종관, ASOS) 시간자료 (data.go.kr 15057210, 1360000/AsosHourlyInfoService).

쓰임: 수질 · 수위 표시가 났을 때 **같은 시각 그 지역에 비가 왔는지** — 자연 원인(강우로 원수 탁도 상승 등)인지,
설비 · 조작 의심인지 가르는 맥락. 경보 보강(enrichment)과 같은 생각이다.

제약 (명세에서 확인):
  - **전날(D-1)까지만** 준다 → 실시간 경보가 아니라 **일일 보고서**용. 실시간이 필요하면 단기예보 초단기실황(격자) 따로
  - 시설 ↔ 관측 지점은 **행정구역 기준 가까운 지점**으로 손으로 정했다 (거리 계산 아님) — STATIONS
  - 지점 번호는 기억이 아니라 **API 응답의 지점명으로 확인**한다: 응답 stnNm 이 기대한 이름으로 시작하지 않으면
    그 지점은 쓰지 않고 보고서에 남긴다 (verify_station)

같은 data.go.kr 키 (이 서비스도 활용 신청 필요).
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow as pa

from . import ingest, store

# 시설관리번호 → (ASOS 지점 번호, 기대 지점명, 근거). 지점명은 API 응답으로 확인한다.
STATIONS = {
    "4219012371": (216, "태백", "황지정수장 태백시"),
    "4273012356": (114, "원주", "송전정수장 횡성군 — 횡성에 ASOS 없음, 원주"),
    "4128112315": (108, "서울", "일산정수장 고양시 — 서울"),
    "4128512319": (108, "서울", "고양정수장 고양시 — 서울"),
    "4136012312": (108, "서울", "와부정수장 남양주시 — 서울"),
    "4136012317": (108, "서울", "덕소정수장 남양주시 — 서울"),
    "4113112313": (108, "서울", "성남정수장 성남시 — 서울"),
    "4127112311": (119, "수원", "반월정수장 안산시 — 수원"),
    "4127312314": (119, "수원", "시흥정수장 안산시 — 수원"),
    "4146512316": (119, "수원", "수지정수장 용인시 — 수원"),
    "4148012318": (99, "파주", "파주정수장 파주시"),
    "4831012332": (294, "거제", "구천정수장 거제시"),
    "4831012331": (294, "거제", "연초정수장 거제시"),
    "4827012381": (288, "밀양", "밀양정수장 밀양시"),
    "4824012333": (192, "진주", "사천정수장 사천시 — 진주"),
    "4833012382": (257, "양산", "양산정수장 양산시"),
    "4812312335": (155, "창원", "반송정수장 창원시"),
    "4783012344": (143, "대구", "고령정수장 고령군 — 대구"),
    "4719012337": (279, "구미", "구미정수장 구미시"),
    "4782012340": (143, "대구", "운문정수장 청도군 — 대구"),
    "4729012339": (143, "대구", "자인정수장 경산시 — 대구"),
    "3171012336": (152, "울산", "온산정수장 울주군"),
    "4684012366": (165, "목포", "대불정수장 무안군 — 목포"),
    "4615012368": (174, "순천", "별량정수장 순천시"),
    "4688012387": (156, "광주", "평림정수장 장성군 — 광주"),
    "4680012386": (260, "장흥", "덕정정수장 장흥군"),
    "4679012364": (156, "광주", "화순정수장 화순군 — 광주"),
    "4513012365": (140, "군산", "군산정수장 군산시"),
    "4580012360": (243, "부안", "부안정수장 부안군"),
    "4571012367": (146, "전주", "고산정수장 완주군 — 전주"),
    "4574012385": (248, "장수", "동화정수장 장수군"),
    "4518012363": (245, "정읍", "산성정수장 정읍시"),
    "4415012357": (239, "세종", "공주정수장 공주시 — 세종"),
    "4471012372": (238, "금산", "금산정수장 금산군"),
    "4476012351": (236, "부여", "석성정수장 부여군"),
    "4420012354": (232, "천안", "아산정수장 아산시 — 천안"),
    "4413112355": (232, "천안", "천안정수장 천안시"),
    "4311112353": (131, "청주", "청주정수장 청주시"),
    "4313012380": (127, "충주", "충주정수장 충주시"),
    "4418012359": (235, "보령", "보령정수장 보령시"),
    "4711312378": (138, "포항", "학야정수장 포항시"),
}

SCHEMA = pa.schema([
    ("ts", pa.timestamp("s")),
    ("stn_id", pa.int32()),
    ("stn_name", pa.string()),
    ("rain_mm", pa.float64()),        # None = 응답에 값 없음 (무강수인지 결측인지는 rain_raw 로)
    ("rain_raw", pa.string()),        # 응답 원래 값 그대로 (빈 문자열 · 0 · 숫자 — 의미 확인 전이라 남겨 둔다)
    ("rain_qc", pa.string()),         # rnQcflag (품질검사 플래그)
    ("source", pa.string()),
    ("flags", pa.string()),
])


def key(r: dict) -> tuple:
    return (r["stn_id"], r["ts"])


def _url() -> str:
    return os.environ.get("KMA_ASOS_URL", "https://apis.data.go.kr/1360000/AsosHourlyInfoService/getWthrDataList")


def fetch_day(stn_id: int, day: date, rows: int = 50) -> list[dict]:
    """하루(00~23시) 시간 자료. 기상청 tm 은 'YYYY-MM-DD HH:MM' 꼴."""
    key_ = os.environ.get("KWATER_API_KEY")                 # data.go.kr 키는 계정 하나에 하나
    if not key_:
        raise RuntimeError("KWATER_API_KEY 가 없음")
    d = day.strftime("%Y%m%d")
    params = {"serviceKey": key_, "pageNo": 1, "numOfRows": rows, "dataType": "JSON", "dataCd": "ASOS",
              "dateCd": "HR", "startDt": d, "startHh": "00", "endDt": d, "endHh": "23", "stnIds": str(stn_id)}
    body = ingest._get(_url(), params, key_)
    if not body.strip():
        raise RuntimeError("기상청 API 빈 응답")
    out = []
    for it in ingest._items_from_response(body):
        raw = ingest._text(it.get("rn"))
        out.append({"ts": ingest.parse_ts(it.get("tm")), "stn_id": int(it.get("stnId") or stn_id),
                    "stn_name": ingest._text(it.get("stnNm")), "rain_mm": ingest.to_float(raw), "rain_raw": raw,
                    "rain_qc": ingest._text(it.get("rnQcflag")), "source": "kma_asos", "flags": ""})
    return out


def verify_station(rows: list[dict], expected: str) -> bool:
    names = {r["stn_name"] for r in rows if r["stn_name"]}
    return bool(names) and all(n.startswith(expected) for n in names)


def collect_day(out_dir, day: date) -> dict:
    """그날 쓰는 모든 지점을 받아 curated_weather 에 저장. 지점명이 안 맞는 지점은 버리고 보고."""
    wanted = {}
    for code, (stn, name, _) in STATIONS.items():
        wanted[stn] = name
    kept, bad, errors = [], {}, {}
    for stn, name in sorted(wanted.items()):
        try:
            rows = fetch_day(stn, day)
        except Exception as e:
            errors[stn] = f"{type(e).__name__}: {e}"[:200]
            if any(s in errors[stn] for s in ("SERVICE_KEY", "ACCESS DENIED", "NOT_REGISTERED", "LIMITED")):
                break                                        # 활용 신청 전 · 한도 초과 — 나머지 지점도 같으니 그만
            continue
        if not verify_station(rows, name):
            bad[stn] = {"expected": name, "got": sorted({r['stn_name'] for r in rows})}
            continue
        kept += [r for r in rows if r["ts"] is not None]
    paths = store.write_orc(kept, out_dir, schema=SCHEMA, subdir="curated_weather", key=key)
    raw_values = sorted({r["rain_raw"] for r in kept if r["rain_mm"] is None})
    report = {"day": day.isoformat(), "stations": len(wanted), "ok_stations": len(wanted) - len(bad) - len(errors),
              "rows": len(kept), "station_mismatch": bad, "errors": errors,
              "rain_hours": sum(1 for r in kept if (r["rain_mm"] or 0) > 0),
              "empty_rain_raw_values": raw_values}
    d = Path(out_dir) / "state"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "weather_runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False) + "\n")
    return report


def due_day(out_dir, today: date) -> date | None:
    """어제 자료를 아직 안 받았으면 어제. 받았으면 None (하루에 한 번)."""
    y = today - timedelta(days=1)
    p = Path(out_dir) / "state" / "weather_runs.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r.get("day") == y.isoformat() and r.get("ok_stations", 0) > 0 and not r.get("errors"):
                return None
    return y
