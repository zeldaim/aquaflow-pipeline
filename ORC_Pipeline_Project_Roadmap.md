# 🗂️ ORC 데이터 파이프라인 프로젝트 로드맵

> **목표**: K-water 수질 공공데이터를 활용한 AWS 기반 ORC 데이터 파이프라인 구축  
> **환경**: LocalStack (무료) + 로컬 스택  
> **난이도**: 입문 → 중급 → 고급 순차 진행

---

## 📁 GitHub 저장소 구조

```
orc-pipeline-project/
│
├── README.md                  # 프로젝트 소개
├── requirements.txt           # 라이브러리 목록
├── .gitignore
│
├── data/                      # 원본 데이터
│   └── raw/                   # K-water 수질 공공데이터
│
├── notebooks/                 # 단계별 실습 노트북
│   ├── 01_basic_pipeline.ipynb
│   ├── 02_automated_pipeline.ipynb
│   └── 03_advanced_pipeline.ipynb
│
├── src/                       # 소스 코드
│   ├── ingestion/             # 데이터 수집
│   ├── transform/             # ORC 변환
│   ├── pipeline/              # 파이프라인
│   └── security/              # 보안 적용
│
├── infra/                     # 인프라 설정
│   ├── localstack/            # LocalStack 설정
│   └── docker-compose.yml
│
├── airflow/                   # Airflow DAG
│   └── dags/
│
└── docs/                      # 문서
    ├── architecture.md
    └── troubleshooting.md
```

---

## 🟢 1단계 — 기초 파이프라인 (입문)

### 목표
공공데이터를 ORC 포맷으로 변환하고 로컬 S3(MinIO)에 적재

### 필요 라이브러리
```bash
pip install pyarrow pandas duckdb boto3 minio requests
```

### 구현 흐름
```
K-water 수질 공공데이터 API 수집
→ Pandas로 전처리·정제
→ PyArrow로 ORC 변환
→ MinIO(로컬 S3)에 적재
→ DuckDB로 ORC 쿼리 실행
→ 결과 시각화 (matplotlib)
```

### 핵심 코드 예시
```python
import pyarrow as pa
import pyarrow.orc as orc
import pandas as pd
from minio import Minio

# 1. 데이터 수집
df = pd.read_csv("kwater_quality.csv")

# 2. ORC 변환
table = pa.Table.from_pandas(df)
orc.write_table(table, "water_quality.orc")

# 3. MinIO 업로드
client = Minio("localhost:9000",
               access_key="minioadmin",
               secret_key="minioadmin",
               secure=False)
client.fput_object("kwater-bucket", 
                   "water_quality.orc", 
                   "water_quality.orc")
```

### 체크리스트
- [ ] 공공데이터포털 API 키 발급
- [ ] MinIO 로컬 설치 및 실행
- [ ] ORC 변환 성공
- [ ] DuckDB 쿼리 실행 성공
- [ ] README 작성

---

## 🟡 2단계 — 자동화 파이프라인 (중급)

### 목표
ETL 자동화 + 파티셔닝 + Airflow 스케줄링 구현

### 추가 라이브러리
```bash
pip install apache-airflow pyspark great-expectations
```

### 구현 흐름
```
Airflow DAG으로 수집 자동화 (일별 스케줄)
→ Spark 로컬 모드로 대용량 ORC 처리
→ 날짜·지역별 파티셔닝
→ Great Expectations로 데이터 품질 검증
→ LocalStack Glue로 메타데이터 관리
→ LocalStack Athena로 파티션 쿼리
→ 이상 감지 시 SNS 알림
```

### 핵심 구현 포인트
```python
# Spark ORC 파티셔닝
df.write \
  .partitionBy("year", "month", "region") \
  .format("orc") \
  .mode("append") \
  .save("s3a://kwater-bucket/partitioned/")

# Great Expectations 데이터 검증
import great_expectations as ge
df_ge = ge.from_pandas(df)
df_ge.expect_column_values_to_not_be_null("water_quality_index")
df_ge.expect_column_values_to_be_between("ph", 6.0, 9.0)
```

### Airflow DAG 구조
```python
from airflow import DAG
from airflow.operators.python import PythonOperator

dag = DAG(
    'kwater_pipeline',
    schedule_interval='@daily',
)

collect = PythonOperator(task_id='collect_data', ...)
transform = PythonOperator(task_id='transform_orc', ...)
validate = PythonOperator(task_id='validate_quality', ...)
load = PythonOperator(task_id='load_to_s3', ...)

collect >> transform >> validate >> load
```

### 체크리스트
- [ ] Airflow 로컬 설치 및 DAG 작성
- [ ] Spark 파티셔닝 구현
- [ ] Great Expectations 검증 룰 설정
- [ ] LocalStack Athena 파티션 쿼리 성공
- [ ] 알림 시스템 구현
- [ ] 아키텍처 다이어그램 작성

---

## 🔴 3단계 — 고급 파이프라인 (고급)

### 목표
실시간 스트리밍 + 레이크하우스 + 보안 + 비용 최적화 완성

### 추가 라이브러리
```bash
pip install kafka-python delta-spark confluent-kafka
```

### 구현 흐름
```
Kafka로 실시간 수질 데이터 스트리밍 수집
→ Spark Structured Streaming으로 실시간 ORC 변환
→ Delta Lake 레이크하우스 구축
   (ACID 트랜잭션, 타임트래블 지원)
→ KMS 암호화 적용 (보안)
→ IAM 최소권한 정책 적용 (보안)
→ S3 Intelligent-Tiering 비용 최적화
→ CloudWatch 모니터링 대시보드
→ 이상 탐지 ML 모델 연동 (사이킷런)
```

### 핵심 구현 포인트
```python
# Spark Structured Streaming + Delta Lake
from delta import *
from pyspark.sql import SparkSession

spark = SparkSession.builder \
    .config("spark.sql.extensions", 
            "io.delta.sql.DeltaSparkSessionExtension") \
    .getOrCreate()

# 실시간 스트리밍 → Delta Lake ORC
stream = spark.readStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", "localhost:9092") \
    .option("subscribe", "kwater-realtime") \
    .load()

stream.writeStream \
    .format("delta") \
    .outputMode("append") \
    .option("checkpointLocation", "/tmp/checkpoint") \
    .start("s3a://kwater-bucket/delta/")
```

### 보안 적용
```python
# IAM 최소권한 정책 예시
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": [
            "s3:GetObject",
            "s3:PutObject"
        ],
        "Resource": "arn:aws:s3:::kwater-bucket/*"
    }]
}
```

### 체크리스트
- [ ] Kafka 로컬 클러스터 구성
- [ ] Spark Streaming 파이프라인 구현
- [ ] Delta Lake 레이크하우스 구축
- [ ] 타임트래블 쿼리 테스트
- [ ] KMS 암호화 적용
- [ ] IAM 최소권한 정책 설계
- [ ] 비용 최적화 설정
- [ ] 모니터링 대시보드 구성
- [ ] ML 이상 탐지 모델 연동
- [ ] 최종 아키텍처 문서화

---

## 📌 GitHub 올리는 법

### 1. 저장소 생성
```bash
# GitHub에서 새 저장소 생성 후
git init
git remote add origin https://github.com/본인계정/orc-pipeline-project.git
```

### 2. .gitignore 설정
```
# .gitignore
*.csv
*.orc
.env
__pycache__/
.ipynb_checkpoints/
*.pyc
venv/
```

### 3. 단계별 커밋 전략
```bash
# 1단계 완성 후
git add .
git commit -m "feat: 1단계 기초 ORC 파이프라인 구현"
git push origin main

# 2단계 완성 후
git commit -m "feat: 2단계 Airflow 자동화 파이프라인 구현"

# 3단계 완성 후
git commit -m "feat: 3단계 Delta Lake 레이크하우스 + 보안 적용"
```

### 4. README.md 필수 포함 내용
```markdown
# ORC 데이터 파이프라인

## 프로젝트 소개
K-water 수질 데이터 기반 AWS ORC 파이프라인

## 아키텍처
(다이어그램 이미지)

## 기술 스택
- LocalStack, MinIO, Spark, Airflow
- Delta Lake, Kafka, DuckDB
- Python, PyArrow

## 실행 방법
1. docker-compose up
2. pip install -r requirements.txt
3. python src/pipeline/main.py

## 주요 성과
- ORC 압축률 XX% 달성
- 쿼리 성능 XX% 개선
- 데이터 품질 검증 XX개 룰 적용
```

---

## 📝 노션 정리 구조

```
📂 포트폴리오
  └── 📄 ORC 데이터 파이프라인 프로젝트
        ├── 🎯 프로젝트 목표
        ├── 🏗️ 아키텍처 다이어그램
        ├── 📊 1단계 결과 (스크린샷)
        ├── 📊 2단계 결과 (스크린샷)
        ├── 📊 3단계 결과 (스크린샷)
        ├── 🔗 GitHub 링크
        ├── 💡 배운 점
        └── 🚀 향후 개선 계획
```

---

## ⚡ 빠른 시작 순서

```
1. GitHub 저장소 생성
2. docker-compose로 LocalStack + MinIO 실행
3. 공공데이터포털에서 K-water 수질 API 키 발급
4. 1단계부터 순서대로 진행
5. 각 단계 완성 시 GitHub 커밋 + 노션 업데이트
```

> 💡 **핵심 원칙**: 완벽하게 하려다 안 하는 것보다, 일단 1단계부터 완성하는 게 중요하다.
