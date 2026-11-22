"""저장 — ORC 파일(날짜별 파티션) · 격리 파일 · 보고서, 그리고 선택적으로 S3 호환 저장소 업로드."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.orc as orc

SCHEMA = pa.schema([
    ("ts", pa.timestamp("s")),
    ("plant_name", pa.string()),
    ("plant_code", pa.string()),
    ("use_kind", pa.string()),
    ("ph", pa.float64()),
    ("turbidity_ntu", pa.float64()),
    ("chlorine_mgl", pa.float64()),
    ("source", pa.string()),
    ("flags", pa.string()),
])


def _table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    cols = {f.name: [r.get(f.name) for r in rows] for f in schema}
    return pa.table(cols, schema=schema)


def _key(r: dict) -> tuple:
    """시설 × 용수 구분 × 시각 (quality.series_key 와 같은 기준 — 생활 · 공업을 따로)."""
    return (r.get("plant_code") or r.get("plant_name") or "", r.get("use_kind") or "", r["ts"])


def write_orc(kept: list[dict], out_dir: str | Path, schema: pa.Schema = SCHEMA, subdir: str = "curated",
              key=_key) -> list[Path]:
    """out_dir/<subdir>/date=YYYY-MM-DD/part-0.orc — 그 날짜 파일이 있으면 **합친다**.
    같은 시설 · 시각은 새로 온 행이 이긴다 (API 가 값을 고쳐 보내는 경우). 그래서 매시간 겹쳐 받아도 중복이 없다.
    수질은 기본값(curated), 수위는 level.py 가 자기 스키마 · 키 · 폴더(curated_level)로 부른다."""
    _key = key
    by_day = defaultdict(list)
    for r in kept:
        by_day[r["ts"].date().isoformat()].append(r)
    paths = []
    for day, rows in sorted(by_day.items()):
        d = Path(out_dir) / subdir / f"date={day}"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "part-0.orc"
        merged = {}
        if p.exists():
            for old in orc.read_table(str(p)).to_pylist():
                merged[_key(old)] = old
        for r in rows:
            merged[_key(r)] = r
        out = sorted(merged.values(), key=_key)
        tmp = d / "part-0.orc.tmp"                          # 쓰다 죽어도 기존 파일이 깨지지 않게
        orc.write_table(_table(out, schema), str(tmp))
        tmp.replace(p)
        paths.append(p)
    return paths


def write_rejected(rejected: list[dict], out_dir: str | Path, append: bool = False) -> Path | None:
    """격리 행 기록. 한 번 넣기(ingest)는 덮어쓰고, 자동 수집(collect)은 이어 쓴다."""
    if not rejected:
        return None
    d = Path(out_dir) / "quarantine"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "rejected.jsonl"
    with p.open("a" if append else "w", encoding="utf-8") as f:
        for r in rejected:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    return p


def write_report(report: dict, out_dir: str | Path) -> Path:
    p = Path(out_dir) / "quality_report.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def upload_s3(paths: list[Path], out_dir: str | Path) -> list[str]:
    """S3 호환 저장소에 경로 구조 그대로 올린다 — LocalStack · MinIO · 실제 AWS S3 모두 같은 코드.
    S3_ENDPOINT (없으면 실제 AWS) · AWS_ACCESS_KEY_ID · AWS_SECRET_ACCESS_KEY · AWS_DEFAULT_REGION · S3_BUCKET (.env)"""
    import boto3

    s3 = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT") or None)
    bucket = os.environ.get("S3_BUCKET", "kwater")
    existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    if bucket not in existing:
        region = os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2")
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
    base = Path(out_dir)
    keys = []
    for p in paths:
        key = p.relative_to(base).as_posix()
        s3.upload_file(str(p), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})   # 저장 시 암호화
        keys.append(key)
    return keys
