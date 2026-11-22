# AquaFlow Pipeline

## 프로젝트 소개

K-water 수질 데이터 기반 ORC 데이터 파이프라인.
정수장 1시간 수질(잔류염소 · pH · 탁도)을 모아 **저장 전에 품질을 검증**하고, ORC 로 저장해 DuckDB 로 조회한다.

```
data.go.kr CSV · Open API ─▶ 수집 ─▶ 품질 검증 ─┬▶ ORC (날짜별) ─▶ S3 호환 저장소 (LocalStack · 저장 시 암호화)
                                                 │        └──────▶ DuckDB 조회
                                                 └▶ 격리 (불가능 값 · 중복 · 미래 시각 · 빈 값) + 품질 보고서
```

**왜 저장 전 검증인가:** 이 데이터는 이상 탐지 모델의 "정상" 학습 데이터로 쓸 예정이다. 조작되거나 깨진 값이 섞이면 모델이 그걸 정상으로 배운다(학습 데이터 오염). 그래서 불가능한 값은 **격리**하고, 가능하지만 기준을 넘는 값은 실제 사건일 수 있으니 **저장하고 표시만** 한다.

## 기술 스택

Python, PyArrow, DuckDB, LocalStack (S3), boto3 — MinIO 는 2025년부터 커뮤니티 도커 이미지를 배포하지 않아 기본 저장소를 LocalStack 으로 바꿨다. 코드는 S3 호환이면 그대로 (MinIO · 실제 AWS 는 `S3_ENDPOINT` 만 바꿈).

## 데이터

| 경로 | data.go.kr | 키 |
|---|---|---|
| CSV 파일 | 「한국수자원공사_실시간 수도정보 수질(시간)」 파일데이터 (15055259) — 사이트에서 받아 `data/raw/` 에 | **필요 없음** |
| Open API | 「한국수자원공사_실시간 수도정보 수질(시간) 조회 서비스」 (15057290, `B500001/rwis/waterQuality`) | 활용 신청 → `.env` 의 `KWATER_API_KEY` |

- CSV 머리글이 예상과 다르면 어느 열이 없는지 알려 준다 → `src/aquaflow/ingest.py` 의 `CSV_ALIASES` 에 이름을 더한다. 인코딩은 UTF-8 · CP949 둘 다 받는다.
- API 작업별 경로는 명세 페이지에 기본 주소만 보여 **확인이 필요하다** — `KWATER_QUALITY_URL` 로 바꿀 수 있다.
- 원본 데이터는 git 에 올리지 않는다 (`data/` 는 `.gitignore`).

## 실행 방법

Docker 로 돌린다 (pyarrow 의 ORC 가 Windows 에서 안 되는 경우가 있어 리눅스 컨테이너).

```bash
cp .env.example .env                       # API 를 쓰면 KWATER_API_KEY 채우기
docker compose up -d s3                    # LocalStack S3 (127.0.0.1:4566)

docker compose run --rm aquaflow ingest --csv data/raw/<받은 파일>.csv --upload
docker compose run --rm aquaflow ingest --api --from 2026-11-01 --to 2026-11-07 --upload
docker compose run --rm aquaflow query     # 정수장별 요약
docker compose run --rm aquaflow query --sql "SELECT * FROM wq WHERE flags <> '' LIMIT 20"

docker compose run --rm --entrypoint pytest aquaflow -q    # 시험
docker compose down -v
```

결과: `data/lake/curated/date=YYYY-MM-DD/part-0.orc` · `data/lake/quarantine/rejected.jsonl` · `data/lake/quality_report.json`. 같은 날짜를 다시 넣으면 그 날짜 파일을 덮어써서 중복이 생기지 않는다.

## 품질 검증 규칙 (`src/aquaflow/quality.py`)

| 구분 | 규칙 | 처리 |
|---|---|---|
| 격리 | 시각을 읽을 수 없음 · 2시간 넘는 미래 · 시설명 없음 · 값 셋 다 비어 있음 · 물리적으로 불가능 (pH 0~14 밖, 탁도 0~1000 NTU 밖, 잔류염소 0~20 mg/L 밖) · 같은 시설 · 시각 중복 | 저장 안 함 |
| 표시 | 기준 초과 (pH 5.8~8.5 밖 · 탁도 0.5 NTU 초과 · 잔류염소 4.0 mg/L 초과) · 한 시간 사이 급변 (pH 1.0 · 탁도 0.3 · 잔류염소 0.5 넘게) | 저장하고 `flags` 에 표시 |

- **기준값은 연습용 출발점이다.** 먹는물 수질기준 최신판으로 확인하고 `THRESHOLDS` 를 고친다.
- `24시` 표기(`YYYYMMDD24`)는 다음 날 0시로 읽는다.

## 확인한 것 (2026-11-22)

- 시험 7개 통과 — `tests/fixtures/synthetic_quality.csv` (**합성 데이터 · 실제 시설 아님**, 일부러 넣은 문제 7가지)
- 같은 합성 데이터로 LocalStack 까지: 10행 → 격리 4 (중복 · 불가능 pH · 빈 값 · 미래 시각) · 저장 6 → ORC 2개 업로드 (S3 저장 시 암호화 `AES256` 확인) → DuckDB 요약
- **실제 K-water 데이터로는 아직 안 돌렸다** (CSV 다운로드 또는 API 키 필요)

## 다음

- 실제 데이터 넣기 (CSV 먼저) · 품질 보고서로 실제 데이터의 격리 · 표시 비율 확인
- 로드맵(`ORC_Pipeline_Project_Roadmap.md`) 2단계: 일별 자동 수집 · 날짜/시설 파티션 · 이상 알림
