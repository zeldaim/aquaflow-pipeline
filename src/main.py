"""AquaFlow 실행

  python src/main.py ingest --csv data/raw/수질.csv          # API 키 없이 (data.go.kr 에서 받은 CSV)
  python src/main.py ingest --api --from 2026-11-01 --to 2026-11-07 [--plant 4824012333]
  python src/main.py ingest ... --upload                      # S3 호환 저장소(LocalStack · MinIO · AWS)에도 올림 (.env)
  python src/main.py query                                     # 정수장별 요약 (DuckDB)
  python src/main.py query --sql "SELECT ... FROM wq"

결과는 data/lake/ (curated/date=*/part-0.orc · quarantine/rejected.jsonl · quality_report.json)
"""
from __future__ import annotations

import argparse
import json
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
    paths = store.write_orc(kept, a.out)
    store.write_rejected(rejected, a.out)
    store.write_report(report, a.out)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"ORC {len(paths)}개 → {a.out}/curated")
    if a.upload:
        keys = store.upload_s3(paths, a.out)
        print(f"S3 업로드 {len(keys)}개: {', '.join(keys)}")
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

    pq = sub.add_parser("query")
    pq.add_argument("--sql", help="테이블 이름은 wq")

    a = p.parse_args(argv)
    if a.cmd == "ingest" and a.api and not (a.date_from and a.date_to):
        p.error("--api 는 --from 과 --to 가 필요")
    return cmd_ingest(a) if a.cmd == "ingest" else cmd_query(a)


if __name__ == "__main__":
    sys.exit(main())
