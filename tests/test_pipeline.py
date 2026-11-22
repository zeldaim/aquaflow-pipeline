"""파이프라인 시험 — tests/fixtures 의 합성 데이터(실제 시설 아님)로만 돈다.

fixture 의 일부러 넣은 문제:
  A 02시 탁도 0.8   → 기준 초과 · 급변 표시 (저장은 함)
  A 03시 잔류염소 0.79→1.60 · 탁도 되돌아옴 → 급변 표시
  A 03시 두 번       → 중복 격리
  A 04시 pH 15.2     → 불가능 값 격리
  A 24시             → 다음 날 00시로
  B 01시 값 전부 비어 있음 → 격리
  B 2099년           → 미래 시각 격리
"""
from datetime import datetime
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aquaflow import ingest, quality, query, store  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_quality.csv"
NOW = datetime(2026, 11, 22)


@pytest.fixture
def rows():
    return ingest.read_csv(FIXTURE)


def test_parse_ts_hour_24_is_next_day():
    assert ingest.parse_ts("2026111824") == datetime(2026, 11, 19, 0)
    assert ingest.parse_ts("2026-11-18 13:00") == datetime(2026, 11, 18, 13)
    assert ingest.parse_ts("") is None


def test_read_csv_maps_korean_header(rows):
    assert len(rows) == 10
    r = rows[0]
    assert r["plant_code"] == "TEST000001" and r["ph"] == 7.10 and r["chlorine_mgl"] == 0.80
    assert r["source"] == "csv:synthetic_quality.csv"


def test_read_csv_cp949(tmp_path):
    p = tmp_path / "cp949.csv"
    p.write_bytes(FIXTURE.read_text(encoding="utf-8").encode("cp949"))
    assert len(ingest.read_csv(p)) == 10


def test_read_csv_real_export_format(tmp_path):
    """실제 내려받은 파일과 같은 머리글 · 기준치 줄 (값은 합성). 기준치 줄은 건너뛰고, 잔류염소 0.05 는 하한 미달 표시."""
    src = Path(__file__).parent / "fixtures" / "synthetic_export_format.csv"
    p = tmp_path / "export_cp949.csv"
    p.write_bytes(src.read_text(encoding="utf-8").encode("cp949"))     # 실제 파일처럼 CP949
    rows = ingest.read_csv(p)
    assert len(rows) == 3 and rows[0]["use_kind"] == "생활정수" and rows[0]["ph"] == 7.60
    assert ingest.read_csv.standards_row == {"ph": "5.8~8.5", "turbidity_ntu": "0.5이하", "chlorine_mgl": "0.1~4.0"}
    kept, rejected, report = quality.check(rows, now=datetime(2026, 11, 22, 12))   # 데이터가 22일 새벽이라 기준 시각도 22일
    assert report["rejected_rows"] == 0
    flags = {r["ts"].hour: r["flags"] for r in kept}
    assert flags[2] == "chlorine_mgl_under_standard,chlorine_mgl_jump"   # 0.83 → 0.05 → 0.84
    assert flags[3] == "chlorine_mgl_jump"


def test_read_csv_missing_column(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("시설명,pH\n가,7\n", encoding="utf-8")
    with pytest.raises(ValueError, match="필요한 열"):
        ingest.read_csv(p)


def test_quality_split(rows):
    kept, rejected, report = quality.check(rows, now=NOW)
    assert report["rejected_rows"] == 4
    assert report["reject_reasons"] == {"duplicate": 1, "ph_impossible": 1, "all_values_missing": 1, "ts_future": 1}
    assert report["kept_rows"] == 6 and report["plants"] == 2
    flags = {(r["plant_code"], r["ts"].hour, r["ts"].day): r["flags"] for r in kept}
    # 02시 탁도 0.042→0.800: 기준 초과 + 급변, 03시 0.800→0.045 탁도 급변 + 잔류염소 0.79→1.60 급변
    assert flags[("TEST000001", 2, 18)] == "turbidity_ntu_over_standard,turbidity_ntu_jump"
    assert flags[("TEST000001", 3, 18)] == "turbidity_ntu_jump,chlorine_mgl_jump"
    assert flags[("TEST000001", 0, 19)] == ""       # 24시 → 다음 날 0시, 앞 시각과 이어지지 않으니 급변 아님


def test_store_and_query(rows, tmp_path):
    kept, rejected, report = quality.check(rows, now=NOW)
    paths = store.write_orc(kept, tmp_path)
    assert sorted(p.parent.name for p in paths) == ["date=2026-11-18", "date=2026-11-19"]
    assert store.write_rejected(rejected, tmp_path).read_text(encoding="utf-8").count("\n") == 4

    result, cols = query.run(tmp_path)
    by_plant = {r[cols.index("plant_name")]: dict(zip(cols, r)) for r in result}
    a = by_plant["테스트정수장A(합성)"]
    assert a["hours"] == 5 and a["flagged_hours"] == 2

    # 다시 돌려도 같은 날짜 파일을 덮어써서 중복이 생기지 않는다
    store.write_orc(kept, tmp_path)
    again, _ = query.run(tmp_path)
    assert sum(r[cols.index("hours")] for r in again) == 6


def test_api_response_json_and_xml():
    js = b'{"response":{"header":{"resultCode":"00"},"body":{"items":{"item":{"occrrncDt":"2026111801","fcltyMngNm":"x","phVal":"7"}}}}}'
    assert ingest._items_from_response(js) == [{"occrrncDt": "2026111801", "fcltyMngNm": "x", "phVal": "7"}]
    xml = b"<response><header><resultCode>00</resultCode></header><body><items><item><phVal>7.1</phVal></item></items></body></response>"
    assert ingest._items_from_response(xml) == [{"phVal": "7.1"}]
    bad = b"<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30</returnReasonCode><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg></cmmMsgHeader></OpenAPI_ServiceResponse>"
    with pytest.raises(RuntimeError, match="SERVICE_KEY"):
        ingest._items_from_response(bad)
