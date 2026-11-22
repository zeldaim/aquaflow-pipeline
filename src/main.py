"""AquaFlow 실행

  python src/main.py ingest --csv data/raw/수질.csv          # API 키 없이 (data.go.kr 에서 받은 CSV)
  python src/main.py ingest --api --from 2026-11-01 --to 2026-11-07 [--plant 4824012333]
  python src/main.py ingest ... --upload                      # S3 호환 저장소(LocalStack · MinIO · AWS)에도 올림 (.env)
  python src/main.py collect --once                            # 자동 수집 한 번 (최근 6시간)
  python src/main.py collect --every 3600 --upload             # 매시간 반복 (docker compose --profile auto)
  python src/main.py query                                     # 정수장별 요약 (DuckDB)
  python src/main.py query --sql "SELECT ... FROM wq"

결과는 data/lake/ (curated/date=*/part-0.orc · quarantine/rejected.jsonl · quality_report.json)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aquaflow import ingest, quality, query, store  # noqa: E402


def load_dotenv(path: Path) -> None:
    """의존성 없이 .env 읽기 (KEY=VALUE 줄만). 이미 있는 환경 변수는 덮지 않는다."""
    import os
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cmd_ingest(a) -> int:
    if a.csv:
        rows = ingest.read_csv(a.csv)
    else:
        rows = ingest.fetch_api(a.date_from, a.date_to, a.plant)
    kept, rejected, report = quality.check(rows)
    if a.csv and ingest.read_csv.standards_row:              # 파일에 적힌 기준치 — quality.THRESHOLDS 와 맞는지 눈으로 확인용
        report["file_standards_row"] = ingest.read_csv.standards_row
    paths = store.write_orc(kept, a.out)
    store.write_rejected(rejected, a.out)
    store.write_report(report, a.out)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"ORC {len(paths)}개 → {a.out}/curated")
    if a.upload:
        keys = store.upload_s3(paths, a.out)
        print(f"S3 업로드 {len(keys)}개: {', '.join(keys)}")
    return 0


def cmd_collect(a) -> int:
    from aquaflow import collect
    a.hours = a.hours or int(os.environ.get("AQUAFLOW_HOURS", "6"))   # 절전 · 장애로 빠진 시간을 다음 실행이 메우게
    if a.once:
        print(json.dumps(collect.run_once(a.out, a.hours, a.upload, a.plant), ensure_ascii=False, indent=2, default=str))
    else:
        collect.loop(a.out, a.every, a.hours, a.upload, a.plant)
    return 0


def cmd_query(a) -> int:
    rows, cols = query.run(a.out, a.sql) if a.sql else query.run(a.out)
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join(str(v) for v in r))
    return 0


def main(argv=None) -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    p = argparse.ArgumentParser(description="AquaFlow — K-water 수질 파이프라인")
    p.add_argument("--out", default="data/lake", help="결과 폴더")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest")
    src = pi.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="data.go.kr 파일데이터 CSV 경로")
    src.add_argument("--api", action="store_true", help="Open API (KWATER_API_KEY 필요)")
    pi.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    pi.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    pi.add_argument("--plant", help="시설관리번호 (없으면 전체)")
    pi.add_argument("--upload", action="store_true", help="S3 호환 저장소에도 올림 (.env)")

    pc = sub.add_parser("collect", help="자동 수집 (Open API · KWATER_API_KEY 필요)")
    pc.add_argument("--once", action="store_true", help="한 번만 (없으면 --every 초마다 반복)")
    pc.add_argument("--every", type=int, default=3600, help="반복 주기(초), 기본 1시간")
    pc.add_argument("--hours", type=int, default=None,
                    help="한 번에 받을 최근 시간 (앞 주기와 겹치게). 없으면 환경변수 AQUAFLOW_HOURS, 그것도 없으면 6")
    pc.add_argument("--plant", help="시설관리번호 (없으면 전체)")
    pc.add_argument("--upload", action="store_true", help="S3 호환 저장소에도 올림")

    pq = sub.add_parser("query")
    pq.add_argument("--sql", help="테이블 이름은 wq")

    a = p.parse_args(argv)
    if a.cmd == "ingest" and a.api and not (a.date_from and a.date_to):
        p.error("--api 는 --from 과 --to 가 필요")
    return {"ingest": cmd_ingest, "collect": cmd_collect, "query": cmd_query}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
