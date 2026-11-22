"""수집 — K-water 실시간 수도정보 수질(시간)을 표준 행으로.

두 경로:
  1. CSV 파일 (data.go.kr 파일데이터 15055259) — API 키 없이, 사이트에서 직접 받은 파일
  2. Open API (data.go.kr 15057290, B500001/rwis/waterQuality) — API 키 필요 (.env 의 KWATER_API_KEY)

표준 행(dict):
  ts            측정 시각 (datetime, 한국 시간 · 시간 단위)
  plant_name    시설(정수장) 이름
  plant_code    시설관리번호
  use_kind      생활 · 공업
  ph            pH
  turbidity_ntu 탁도 (NTU)
  chlorine_mgl  잔류염소 (mg/L)
  source        "csv:<파일 이름>" 또는 "api"
"""
from __future__ import annotations

import csv
import io
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

# CSV 머리글 → 표준 이름. 실제 파일 머리글이 조금 달라도 받도록 후보를 여럿 둔다.
# (data.go.kr 설명에 적힌 열: 구분, 발생일시, 시설명, 시설관리번호, 시설주소, pH, pH단위, 탁도, 잔류염소, 잔류염소단위, 데이터변경일자)
CSV_ALIASES = {
    "ts": ["발생일시", "occrrncDt"],
    "plant_name": ["시설명", "시설관리명", "fcltyMngNm"],
    "plant_code": ["시설관리번호", "fcltyMngNo"],
    "use_kind": ["구분", "용수구분명", "생활공업구분", "liIndDivName"],
    "ph": ["pH", "PH", "ph", "phVal"],
    "turbidity_ntu": ["탁도", "tbVal"],
    "chlorine_mgl": ["잔류염소", "clVal"],
}
REQUIRED = ["ts", "plant_name", "ph", "turbidity_ntu", "chlorine_mgl"]

# API 응답 필드 → 표준 이름 (data.go.kr 15057290 상세 명세)
API_FIELDS = {
    "occrrncDt": "ts", "fcltyMngNm": "plant_name", "fcltyMngNo": "plant_code",
    "liIndDivName": "use_kind", "phVal": "ph", "tbVal": "turbidity_ntu", "clVal": "chlorine_mgl",
}


def parse_ts(value: str) -> datetime | None:
    """'2015111824'(YYYYMMDDHH, 24시 = 다음 날 0시) · '2026-04-28 13:00' 등 여러 꼴을 받는다."""
    s = (value or "").strip()
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 10:                                   # YYYYMMDDHH
        day = datetime.strptime(digits[:8], "%Y%m%d")
        return day + timedelta(hours=int(digits[8:10]))     # 24 → 다음 날 00시
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H", "%Y/%m/%d %H:%M", "%Y%m%d%H%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def to_float(value) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if s in ("", "-", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):                      # data.go.kr CSV 는 CP949 인 경우가 많다
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    raise ValueError("CSV 인코딩을 알 수 없음 (UTF-8 · CP949 둘 다 아님)")


def _standard(raw: dict, source: str) -> dict:
    return {
        "ts": parse_ts(raw.get("ts", "")),
        "plant_name": (raw.get("plant_name") or "").strip(),
        "plant_code": (raw.get("plant_code") or "").strip(),
        "use_kind": (raw.get("use_kind") or "").strip(),
        "ph": to_float(raw.get("ph")),
        "turbidity_ntu": to_float(raw.get("turbidity_ntu")),
        "chlorine_mgl": to_float(raw.get("chlorine_mgl")),
        "source": source,
    }


def read_csv(path: str | Path) -> list[dict]:
    path = Path(path)
    text = _decode(path.read_bytes())
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    colmap = {}
    for std, cands in CSV_ALIASES.items():
        for c in cands:
            if c in header:
                colmap[std] = c
                break
    missing = [k for k in REQUIRED if k not in colmap]
    if missing:
        raise ValueError(f"CSV 에 필요한 열이 없음: {missing} · 파일 머리글: {header} — ingest.CSV_ALIASES 에 이름을 더하면 된다")
    rows = []
    for r in reader:
        r = {k.strip(): v for k, v in r.items() if k}
        rows.append(_standard({std: r.get(col) for std, col in colmap.items()}, f"csv:{path.name}"))
    return rows


def _items_from_response(body: bytes) -> list[dict]:
    """data.go.kr 응답은 JSON 이거나 XML — 둘 다 response.body.items.item 목록으로 꺼낸다."""
    text = body.decode("utf-8", errors="replace").strip()
    if text.startswith("{"):
        import json
        data = json.loads(text)
        header = data.get("response", {}).get("header", {})
        if header and str(header.get("resultCode", "00")) not in ("00", "0", "INFO-000"):
            raise RuntimeError(f"API 오류: {header}")
        items = data.get("response", {}).get("body", {}).get("items") or {}
        item = items.get("item", []) if isinstance(items, dict) else items
        return [item] if isinstance(item, dict) else list(item)
    root = ET.fromstring(text)
    code = root.findtext(".//resultCode") or root.findtext(".//returnReasonCode")
    if code and code not in ("00", "0"):
        msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg")
        raise RuntimeError(f"API 오류: {code} {msg}")
    return [{child.tag: (child.text or "") for child in it} for it in root.iter("item")]


def fetch_api(st_date: str, ed_date: str, plant_code: str | None = None, rows: int = 1000) -> list[dict]:
    """시작 · 끝 날짜(YYYY-MM-DD) 사이 1시간 수질. 쪽(page)을 끝까지 돈다."""
    import requests

    key = os.environ.get("KWATER_API_KEY")
    if not key:
        raise RuntimeError("KWATER_API_KEY 가 없음 — .env 에 넣는다 (.env.example 참고). 키 없이 하려면 CSV 경로를 쓴다")
    # 명세 페이지에는 기본 주소만 나와 있어 작업별 경로는 확인이 필요하다 — 바꿀 수 있게 환경 변수로 둔다
    url = os.environ.get("KWATER_QUALITY_URL", "https://apis.data.go.kr/B500001/rwis/waterQuality/list")
    out, page = [], 1
    while True:
        params = {"serviceKey": key, "stDt": st_date, "stTm": "00", "edDt": ed_date, "edTm": "24",
                  "numOfRows": rows, "pageNo": page, "_type": "json"}
        if plant_code:
            params["fcltyMngNo"] = plant_code
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        items = _items_from_response(resp.content)
        out += [_standard({API_FIELDS[k]: v for k, v in it.items() if k in API_FIELDS}, "api") for it in items]
        if len(items) < rows:
            return out
        page += 1
