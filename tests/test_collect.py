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
