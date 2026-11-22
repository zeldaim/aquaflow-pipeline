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
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

# CSV 머리글 → 표준 이름. 실제 파일 머리글이 조금 달라도 받도록 후보를 여럿 둔다.
# (data.go.kr 설명에 적힌 열: 구분, 발생일시, 시설명, 시설관리번호, 시설주소, pH, pH단위, 탁도, 잔류염소, 잔류염소단위, 데이터변경일자)
# (실제 내려받은 파일 2026-11-22: 생활/공업 구분, 발생일시, 시설명, 시설관리번호, 시설주소, pH(-), 탁도(NTU), 잔류염소(mg/L), 데이터변경일자
#  — 둘째 줄은 데이터가 아니라 기준치 줄: ",,,,,5.8~8.5,0.5이하,0.1~4.0,")
CSV_ALIASES = {
    "ts": ["발생일시", "occrrncDt"],
    "plant_name": ["시설명", "시설관리명", "fcltyMngNm"],
    "plant_code": ["시설관리번호", "fcltyMngNo"],
    "use_kind": ["생활/공업 구분", "구분", "용수구분명", "생활공업구분", "liIndDivName"],
    "ph": ["pH(-)", "pH", "PH", "ph", "phVal"],
    "turbidity_ntu": ["탁도(NTU)", "탁도", "tbVal"],
    "chlorine_mgl": ["잔류염소(mg/L)", "잔류염소", "clVal"],
}
REQUIRED = ["ts", "plant_name", "ph", "turbidity_ntu", "chlorine_mgl"]

# API 응답 필드 → 표준 이름 (data.go.kr 15057290 상세 명세)
API_FIELDS = {
    "occrrncDt": "ts", "fcltyMngNm": "plant_name", "fcltyMngNo": "plant_code",
    "liIndDivName": "use_kind", "phVal": "ph", "tbVal": "turbidity_ntu", "clVal": "chlorine_mgl",
}


def parse_ts(value: str) -> datetime | None:
    """'2015111824'(YYYYMMDDHH, 24시 = 다음 날 0시) · '2026-04-28 13:00' 등 여러 꼴을 받는다."""
    s = _text(value)                                        # API 는 2026112210 처럼 숫자로 줄 때가 있다
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


def _text(value) -> str:
    """문자열 · 숫자 · None 모두 문자열로 (API JSON 은 시설관리번호 · 시각을 숫자로 준다)."""
    return "" if value is None else str(value).strip()


def _standard(raw: dict, source: str) -> dict:
    return {
        "ts": parse_ts(raw.get("ts")),
        "plant_name": _text(raw.get("plant_name")),
        "plant_code": _text(raw.get("plant_code")),
        "use_kind": _text(raw.get("use_kind")),
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
        if not (r.get(colmap["ts"]) or "").strip() and not (r.get(colmap["plant_name"]) or "").strip():
            # 시각 · 시설이 둘 다 빈 줄 = 기준치 줄 ("5.8~8.5" 등) 이나 빈 줄 — 데이터가 아니라 건너뛴다 (격리 아님)
            note = {std: r.get(colmap[std]) for std in ("ph", "turbidity_ntu", "chlorine_mgl")}
            if any(v and v.strip() for v in note.values()):
                read_csv.standards_row = note
            continue
        rows.append(_standard({std: r.get(col) for std, col in colmap.items()}, f"csv:{path.name}"))
    return rows


read_csv.standards_row = None   # 마지막으로 읽은 파일의 기준치 줄 (있으면) — main 이 보고서에 적는다


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


MAX_PAGES = 200                                             # API 가 pageNo 를 무시해도 끝없이 돌지 않게


def _get(url: str, params: dict, key: str) -> bytes:
    """GET + 재시도. data.go.kr 은 느리다 (2026-11-22 실측: 2행에 12초, 1000행 요청은 30초 넘게).
    오류 메시지에는 요청 주소(=키 포함)가 들어가므로 키를 가린 RuntimeError 로 바꿔 올린다."""
    import requests

    timeout = float(os.environ.get("KWATER_TIMEOUT", "120"))
    tries = int(os.environ.get("KWATER_RETRIES", "3"))
    last = None
    for i in range(tries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except (requests.Timeout, requests.ConnectionError) as e:   # 일시적 — 잠깐 쉬고 다시
            last = e
            time.sleep(5 * (i + 1))
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (502, 503, 504):                               # 게이트웨이 일시 오류 (실측: 504 SERVICETIMEOUT) — 다시
                last = e
                time.sleep(10 * (i + 1))
                continue
            body = e.response.text[:300] if e.response is not None else ""   # 4xx 등 — 다시 해도 같음
            raise RuntimeError(f"API HTTP {e.response.status_code if e.response is not None else '?'}: {body}".replace(key, "***")) from None
    raise RuntimeError(f"API 응답 없음 ({tries}번 시도, 한 번에 {timeout:.0f}초): {type(last).__name__}".replace(key, "***"))


def fetch_api(st_date: str, ed_date: str, plant_code: str | None = None, rows: int = 100,
              st_hour: str = "00", ed_hour: str = "24") -> list[dict]:
    """시작 · 끝 날짜(YYYY-MM-DD) · 시각(00~24) 사이 1시간 수질. 쪽(page)을 끝까지 돈다.
    작업 경로 `/list` 는 2026-11-22 실제 키로 확인 (기본 주소 · 다른 이름은 NO_OPENAPI_SERVICE_ERROR)."""
    key = os.environ.get("KWATER_API_KEY")
    if not key:
        raise RuntimeError("KWATER_API_KEY 가 없음 — .env 에 넣는다 (.env.example 참고). 키 없이 하려면 CSV 경로를 쓴다")
    url = os.environ.get("KWATER_QUALITY_URL", "https://apis.data.go.kr/B500001/rwis/waterQuality/list")
    out, page = [], 1
    while True:
        params = {"serviceKey": key, "stDt": st_date, "stTm": st_hour, "edDt": ed_date, "edTm": ed_hour,
                  "numOfRows": rows, "pageNo": page, "_type": "json"}
        if plant_code:
            params["fcltyMngNo"] = plant_code
        body = _get(url, params, key)
        if not body.strip():
            # 2026-11-22 실측: 응답이 크면(6시간 × 300행 ≈ 80KB) 오류 없이 HTTP 200 · 빈 본문이 온다.
            # 쪽 크기를 반으로 줄여 처음부터 다시 (쪽 번호가 쪽 크기에 묶여 있어서)
            if rows <= 10:
                raise RuntimeError("API 가 빈 응답만 준다 (쪽 크기 10 에서도)")
            rows //= 2
            out, page = [], 1
            continue
        items = _items_from_response(body)
        out += [_standard({API_FIELDS[k]: v for k, v in it.items() if k in API_FIELDS}, "api") for it in items]
        if len(items) < rows:
            return out
        page += 1
        if page > MAX_PAGES:
            raise RuntimeError(f"쪽이 {MAX_PAGES} 을 넘음 — 기간을 줄이거나 API 가 pageNo 를 받는지 확인")


def now_kst() -> datetime:
    """K-water 시각은 한국 시간. 컨테이너 시계는 UTC 라 고정 +9시간으로 맞춘다 (tzdata 없이)."""
    from datetime import timezone
    return datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None)
