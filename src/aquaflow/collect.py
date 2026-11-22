"""자동 수집 — 정해진 주기마다 Open API 에서 최근 몇 시간을 받아 검증 · 저장 · 알림.

한 번(run_once):
  1. 지금(한국 시간)부터 hours 시간 전까지 API 로 받음 — 앞 주기와 **겹치게** 받는다 (늦게 올라오는 값 · 고친 값)
  2. 품질 검증 → 격리 행은 quarantine/rejected.jsonl 에 이어 쓰기
  3. 날짜별 ORC 에 합치기 (같은 시설 · 시각은 새 값이 이김 → 겹쳐 받아도 중복 없음)
  4. 새로 표시(기준 초과 · 급변)된 행만 알림 — 이미 보낸 건 state/alerts_sent.json 으로 거름
  5. (선택) S3 업로드
  6. state/collect_runs.jsonl 에 한 줄 (언제 · 몇 행 · 실패 이유)

반복(loop): 실패해도 멈추지 않고 다음 주기에 다시 — 실패는 기록하고, 연속 실패가 쌓이면 알림.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import ingest, quality, store

ALERT_KEEP_DAYS = 7          # 이미 보낸 알림 기억 기간
FAIL_ALERT_AFTER = 3         # 연속 실패가 이만큼이면 알림


def window(now: datetime, hours: int) -> tuple[str, str, str, str]:
    """API 인자 (stDt, stTm, edDt, edTm). 끝은 지금 시각(정시)."""
    start = now - timedelta(hours=hours)
    return start.strftime("%Y-%m-%d"), start.strftime("%H"), now.strftime("%Y-%m-%d"), now.strftime("%H")


def _state_dir(out_dir) -> Path:
    d = Path(out_dir) / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_sent(path: Path, now: datetime) -> dict:
    if not path.exists():
        return {}
    sent = json.loads(path.read_text(encoding="utf-8"))
    cutoff = (now - timedelta(days=ALERT_KEEP_DAYS)).isoformat()
    return {k: v for k, v in sent.items() if v >= cutoff}       # 오래된 건 잊는다 (파일이 끝없이 크지 않게)


def notify(text: str) -> bool:
    """ALERT_WEBHOOK_URL 이 있으면 POST ({"text": ...} — Slack 호환). 없으면 출력만. 주소는 로그에 남기지 않는다."""
    print(f"[알림] {text}")
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return False
    import requests
    try:
        requests.post(url, json={"text": text}, timeout=10).raise_for_status()
        return True
    except Exception as e:                                       # 알림 실패가 수집을 멈추면 안 됨
        print(f"[알림 실패] {type(e).__name__}")
        return False


def new_alerts(kept: list[dict], out_dir, now: datetime) -> list[dict]:
    path = _state_dir(out_dir) / "alerts_sent.json"
    sent = _load_sent(path, now)
    fresh = []
    for r in kept:
        if not r["flags"]:
            continue
        key = f"{r['plant_code'] or r['plant_name']}|{r['use_kind']}|{r['ts'].isoformat()}|{r['flags']}"
        if key in sent:
            continue
        sent[key] = now.isoformat()
        fresh.append(r)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sent, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return fresh


def _log_run(out_dir, entry: dict) -> None:
    with (_state_dir(out_dir) / "collect_runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def run_once(out_dir, hours: int = 6, upload: bool = False, plant: str | None = None,
             now: datetime | None = None) -> dict:
    now = (now or ingest.now_kst()).replace(minute=0, second=0, microsecond=0)
    st_d, st_h, ed_d, ed_h = window(now, hours)
    rows = ingest.fetch_api(st_d, ed_d, plant, st_hour=st_h, ed_hour=ed_h)
    kept, rejected, report = quality.check(rows, now=now)
    paths = store.write_orc(kept, out_dir)
    store.write_rejected(rejected, out_dir, append=True)
    alerts = new_alerts(kept, out_dir, now)
    for r in alerts:
        notify(f"수질 표시 {r['plant_name']}({r['use_kind'] or '-'}) {r['ts']:%m-%d %H시} — {r['flags']} "
               f"(pH {r['ph']} · 탁도 {r['turbidity_ntu']} NTU · 잔류염소 {r['chlorine_mgl']} mg/L)")
    summary = {"at": now, "window": f"{st_d} {st_h}시 ~ {ed_d} {ed_h}시", "fetched": len(rows),
               "kept": report["kept_rows"], "rejected": report["rejected_rows"],
               "reject_reasons": report["reject_reasons"], "new_alerts": len(alerts), "orc_files": len(paths)}

    # 수위 — 실패해도 수질 결과는 남긴다 (서비스 · 활용 신청이 따로라 한쪽만 막힐 수 있다)
    if os.environ.get("AQUAFLOW_LEVEL", "1") == "1":
        from . import level
        try:
            lrows = level.fetch(st_d, ed_d, st_h, ed_h)
            lkept, lrej, lrep = level.check(lrows, now)
            lpaths = level.write(lkept, out_dir)
            if lrej:
                (Path(out_dir) / "quarantine").mkdir(parents=True, exist_ok=True)
                with (Path(out_dir) / "quarantine" / "rejected_level.jsonl").open("a", encoding="utf-8") as f:
                    for r in lrej:
                        f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            lalerts = new_alerts([{**r, "plant_code": r["tag"], "plant_name": r["tank"], "use_kind": "수위"} for r in lkept],
                                 out_dir, now)
            for r in lalerts:
                notify(f"수위 급변 {r['facility_name']} · {r['tank']} {r['ts']:%m-%d %H시} — {r['level_m']} m")
            paths += lpaths
            summary["level"] = {"fetched": len(lrows), "kept": lrep["kept_rows"], "rejected": lrep["rejected_rows"],
                                "tanks": lrep["tanks"], "kinds": lrep["kinds"], "new_alerts": len(lalerts)}
        except Exception as e:
            summary["level"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    keys = store.upload_s3(paths, out_dir) if upload and paths else []
    summary.update({"uploaded": len(keys), "ok": True})
    _log_run(out_dir, summary)
    return summary


def loop(out_dir, every_s: int = 3600, hours: int = 6, upload: bool = False, plant: str | None = None,
         max_runs: int | None = None) -> None:
    fails, runs = 0, 0
    while max_runs is None or runs < max_runs:
        runs += 1
        try:
            s = run_once(out_dir, hours, upload, plant)
            print(json.dumps(s, ensure_ascii=False, default=str))
            fails = 0
        except Exception as e:
            fails += 1
            msg = f"{type(e).__name__}: {e}"
            if "serviceKey" in msg:                              # 혹시라도 키가 메시지에 섞이면 가린다
                msg = msg.split("serviceKey")[0] + "serviceKey=***"
            print(f"[수집 실패 {fails}회 연속] {msg}")
            _log_run(out_dir, {"at": ingest.now_kst(), "ok": False, "error": msg})
            if fails == FAIL_ALERT_AFTER:
                notify(f"수질 자동 수집이 {fails}회 연속 실패 — 마지막 오류: {msg[:200]}")
        if max_runs is None or runs < max_runs:
            time.sleep(every_s)
