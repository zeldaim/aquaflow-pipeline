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

### 자동 수집 (Open API · 매시간)

```bash
docker compose run --rm aquaflow collect --once            # 한 번 (최근 6시간)
docker compose --profile auto up -d collector              # 매시간 계속 (restart: unless-stopped)
```

- 매 주기 **최근 6시간을 겹쳐** 받는다 — 늦게 올라오거나 고쳐진 값까지. 날짜별 ORC 에 **합치고** 같은 시설 · 시각은 새 값이 이겨서 중복이 없다 (쓰는 중에 죽어도 기존 파일이 깨지지 않게 임시 파일 → 교체).
- 새로 표시된 행만 알림 (`state/alerts_sent.json` 으로 7일간 기억) · `ALERT_WEBHOOK_URL` 이 있으면 Slack 호환 웹훅.
- 실패해도 멈추지 않고 다음 주기에 다시 · **3회 연속 실패면 알림** · 매 실행은 `state/collect_runs.jsonl` 에 한 줄.
- 오류 메시지에 API 키가 섞이지 않게 가린다 (시험으로 확인).
- 시각은 **한국 시간** 기준 — 컨테이너 시계는 UTC 라 그대로 쓰면 실제 데이터가 9시간 "미래"로 보여 격리된다 (고쳐 둠).
- 크롤링이 아니라 data.go.kr 이 자동 수집용으로 내는 **Open API** 를 쓴다 (사이트 자동 긁기는 이용 약관 · 변경에 약함).

결과: `data/lake/curated/date=YYYY-MM-DD/part-0.orc` · `data/lake/quarantine/rejected.jsonl` · `data/lake/quality_report.json`. 같은 날짜를 다시 넣으면 그 날짜 파일을 덮어써서 중복이 생기지 않는다.

## 품질 검증 규칙 (`src/aquaflow/quality.py`)

| 구분 | 규칙 | 처리 |
|---|---|---|
| 격리 | 시각을 읽을 수 없음 · 2시간 넘는 미래 · 시설명 없음 · 값 셋 다 비어 있음 · 물리적으로 불가능 (pH 0~14 밖, 탁도 0~1000 NTU 밖, 잔류염소 0~20 mg/L 밖) · 같은 시설 · 시각 중복 | 저장 안 함 |
| 표시 | 기준 벗어남 (pH 5.8~8.5 밖 · 탁도 0.5 NTU 초과 · 잔류염소 0.1~4.0 mg/L 밖) · 한 시간 사이 급변 (pH 1.0 · 탁도 0.3 · 잔류염소 0.5 넘게) | 저장하고 `flags` 에 표시 (`*_over_standard` · `*_under_standard` · `*_jump`) |

- 기준치는 K-water 내려받은 파일의 기준치 줄과 같다. 급변 기준은 출발점일 뿐이다 (아래 "알게 된 것").
- `24시` 표기(`YYYYMMDD24`)는 다음 날 0시로 읽는다.

## 확인한 것 (2026-11-22)

- 시험 14개 통과 — 합성 데이터(**실제 시설 아님**) `tests/fixtures/` 2개 · 가짜 API 서버 `tests/fake_api.py`
  - 수집 · 검증 · ORC · DuckDB, 실제 파일 꼴(CP949 · 기준치 줄), 자동 수집(겹쳐 받아도 중복 없음 · 같은 알림 한 번 · 페이지 넘김 · 잘못된 키 3회 연속 실패 알림 · 키가 출력에 안 나옴)
- 합성 데이터로 LocalStack 까지: 10행 → 격리 4 · 저장 6 → ORC 업로드 (S3 저장 시 암호화 `AES256` 확인) → DuckDB 요약
- **실제 K-water 데이터 (CSV, 반월정수장 2026-11-22 01~16시 16행):** 격리 0 · 표시 0. pH 7.61~7.64 · 탁도 0.0559~0.0573 NTU · 잔류염소 0.77~0.93 mg/L
  - 실제 파일은 예상과 머리글이 달랐고(`pH(-)` · `탁도(NTU)` · `잔류염소(mg/L)`), **둘째 줄이 기준치 줄**(`5.8~8.5 · 0.5이하 · 0.1~4.0`)이었다 → 머리글 이름 추가, 기준치 줄은 건너뛰고 보고서에 적음, 잔류염소 **하한 0.1** 추가 (낮으면 소독 부족)
  - **알게 된 것:** 실제 값은 한 시간에 pH ±0.02 · 탁도 ±0.001 정도로 아주 안정적이다. 지금 급변 기준(pH 1.0 · 탁도 0.3)은 이보다 수십 배 느슨해서 작은 조작은 못 잡는다 → 데이터가 쌓이면 시설별 실제 변동 폭에서 기준을 정해야 한다 (16행으로는 부족)
- Open API 로는 아직 안 돌렸다 (키 필요) — 응답 꼴 · 작업별 경로는 키를 받은 뒤 확인

## 다음

- Open API 키로 자동 수집 돌리기 → 여러 정수장 · 며칠치 쌓기
- 쌓인 데이터로 시설별 정상 변동 폭 → 급변 기준 · 이상 탐지 모델의 정상 학습 데이터
- 로드맵(`ORC_Pipeline_Project_Roadmap.md`) 2단계 나머지: 시설별 파티션 · 대량 처리
