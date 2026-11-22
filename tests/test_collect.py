"""자동 수집 시험 — 가짜 API 서버(tests/fake_api.py, 합성 데이터)로."""
from datetime import datetime
from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aquaflow import collect, ingest, query  # noqa: E402
import fake_api  # noqa: E402


@pytest.fixture
def api(monkeypatch):
    srv, url = fake_api.start()
    fake_api.Handler.calls.clear()
    monkeypatch.setenv("KWATER_API_KEY", "fake-key")
    monkeypatch.setenv("KWATER_QUALITY_URL", url)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("KWATER_RETRIES", "1")                                    # 시험은 재시도 없이 빨리
    monkeypatch.setenv("AQUAFLOW_LEVEL", "0")                                    # 수위는 따로 시험 (아래)
    monkeypatch.setenv("AQUAFLOW_WEATHER", "0")                                  # 강수량도 따로
    monkeypatch.setenv("KMA_ASOS_URL", url.replace("waterQuality", "AsosHourlyInfoService/getWthrDataList"))
    monkeypatch.setenv("KWATER_LEVEL_URL", url.replace("waterQuality", "waterLevel"))
    yield
    srv.shutdown()


def test_numeric_json_fields():
    """실제 API 는 시설관리번호 · 시각 · 값을 숫자로 준다 (2026-11-22 확인)."""
    r = ingest._standard({"ts": 2026112210, "plant_code": 3171012336, "plant_name": "x", "ph": 7.1,
                          "turbidity_ntu": 0.05, "chlorine_mgl": 0.0451}, "api")
    assert r["ts"] == datetime(2026, 11, 22, 10) and r["plant_code"] == "3171012336" and r["chlorine_mgl"] == 0.0451


def test_window_crosses_midnight():
    assert collect.window(datetime(2026, 11, 22, 3), 6) == ("2026-11-21", "21", "2026-11-22", "03")


def test_now_kst_is_utc_plus_9():
    from datetime import timezone
    diff = ingest.now_kst() - datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(diff.total_seconds() - 9 * 3600) < 5


def test_overlapping_runs_no_duplicates_and_alert_once(api, tmp_path, capsys):
    s1 = collect.run_once(tmp_path, hours=6, now=datetime(2026, 11, 22, 10))     # 04~10시
    # 7시각 × 2곳. 알림 2: A 09시(기준 초과 + 급변 0.05→0.9) · A 10시(급변 0.9→0.05)
    assert s1["fetched"] == 14 and s1["kept"] == 14 and s1["new_alerts"] == 2
    s2 = collect.run_once(tmp_path, hours=6, now=datetime(2026, 11, 22, 12))     # 06~12시 (06~10 겹침)
    assert s2["fetched"] == 14 and s2["new_alerts"] == 0                         # 같은 표시는 다시 안 알림

    rows, cols = query.run(tmp_path)
    assert sum(r[cols.index("hours")] for r in rows) == 18                       # 04~12시 9시각 × 2곳, 겹침 중복 없음
    runs = (tmp_path / "state" / "collect_runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(runs) == 2 and all(json.loads(l)["ok"] for l in runs)
    assert "[알림] 수질 표시 가짜정수장A(합성)(-) 11-22 09시" in capsys.readouterr().out

    call = fake_api.Handler.calls[0]
    assert (call["stDt"], call["stTm"], call["edDt"], call["edTm"]) == ("2026-11-22", "04", "2026-11-22", "10")


def test_paging(api, tmp_path, monkeypatch):
    real = ingest.fetch_api
    monkeypatch.setattr(ingest, "fetch_api", lambda *a, **k: real(*a, **{**k, "rows": 5}))
    s = collect.run_once(tmp_path, hours=6, now=datetime(2026, 11, 22, 10))
    assert s["fetched"] == 14 and len(fake_api.Handler.calls) == 3               # 5 + 5 + 4


def test_level_collected_per_tank(api, tmp_path, monkeypatch):
    """수위: 탱크(태그)마다 시계열. T1 09시 4.5→7.5→4.5 m 급변 두 번 알림, T2 는 조용."""
    monkeypatch.setenv("AQUAFLOW_LEVEL", "1")
    s = collect.run_once(tmp_path, hours=6, now=datetime(2026, 11, 22, 10))
    lv = s["level"]
    assert lv["kept"] == 14 and lv["tanks"] == 2 and lv["kinds"] == {"배수지": 14} and lv["new_alerts"] == 2
    s2 = collect.run_once(tmp_path, hours=6, now=datetime(2026, 11, 22, 12))
    assert s2["level"]["new_alerts"] == 0
    from aquaflow import level
    import pyarrow.orc as orc
    t = orc.read_table(str(tmp_path / "curated_level" / "date=2026-11-22" / "part-0.orc")).to_pylist()
    assert len(t) == 18 and {r["tag"] for r in t} == {"T1", "T2"}          # 04~12시 × 2탱크, 겹침 중복 없음


def test_weather_daily_once_and_station_check(api, tmp_path, monkeypatch):
    """강수량: 어제(D-1) 하루 · 지점명 확인(99 는 엉뚱한 이름 → 안 씀) · 하루에 한 번만 · 수질과 JOIN."""
    from aquaflow import weather
    monkeypatch.setenv("AQUAFLOW_WEATHER", "1")
    s = collect.run_once(tmp_path, hours=6, now=datetime(2026, 11, 22, 10))
    n_st = len({v[0] for v in weather.STATIONS.values()})
    w = s["weather"]
    assert w["day"] == "2026-11-21" and w["stations"] == n_st and w["ok_stations"] == n_st - 1
    assert w["rows"] == (n_st - 1) * 24 and w["rain_hours"] == 1
    assert w["problems"]["mismatch"] == {99: {"expected": "파주", "got": ["엉뚱"]}}
    s2 = collect.run_once(tmp_path, hours=6, now=datetime(2026, 11, 22, 11))
    assert "weather" not in s2                                                   # 어제 것은 이미 받음
    rows, cols = query.run(tmp_path, "SELECT stn_name, rain_mm, rain_raw FROM wx WHERE rain_mm > 0")
    assert rows == [("태백", 3.5, "3.5")]


def test_level_failure_does_not_stop_quality(api, tmp_path, monkeypatch):
    monkeypatch.setenv("AQUAFLOW_LEVEL", "1")
    monkeypatch.setenv("KWATER_LEVEL_URL", "http://127.0.0.1:9/closed")
    s = collect.run_once(tmp_path, hours=6, now=datetime(2026, 11, 22, 10))
    assert s["ok"] and s["kept"] == 14 and s["level"]["ok"] is False and "fake-key" not in s["level"]["error"]


def test_empty_body_shrinks_page_size(api, tmp_path, monkeypatch):
    """실제 API: 응답이 크면 HTTP 200 · 빈 본문. 쪽 크기를 반으로 줄여 다시 받는다."""
    monkeypatch.setattr(fake_api.Handler, "empty_above", 20)
    rows = ingest.fetch_api("2026-11-22", "2026-11-22", st_hour="04", ed_hour="10", rows=100)
    assert len(rows) == 14
    sizes = [int(c["numOfRows"]) for c in fake_api.Handler.calls]
    assert sizes[:4] == [100, 50, 25, 12]                       # 100 · 50 · 25 는 빈 응답 → 12 로 받음


def test_bad_key_fails_and_alerts_after_3(api, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KWATER_API_KEY", "wrong")
    collect.loop(tmp_path, every_s=0, hours=1, max_runs=3)
    out = capsys.readouterr().out
    assert out.count("[수집 실패") == 3
    assert "[알림] 수질 자동 수집이 3회 연속 실패" in out and "SERVICE_KEY_IS_NOT_REGISTERED" in out
    runs = [json.loads(l) for l in (tmp_path / "state" / "collect_runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["ok"] for r in runs] == [False, False, False]


def test_key_never_printed(api, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KWATER_API_KEY", "fake-key")
    monkeypatch.setenv("KWATER_QUALITY_URL", "http://127.0.0.1:9/closed")         # 연결 실패 → 오류 메시지에 URL
    collect.loop(tmp_path, every_s=0, hours=1, max_runs=1)
    out = capsys.readouterr().out + (tmp_path / "state" / "collect_runs.jsonl").read_text(encoding="utf-8")
    assert "fake-key" not in out
