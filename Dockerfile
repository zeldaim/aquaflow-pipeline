# pyarrow 의 ORC 는 Windows 빌드에서 쓸 수 없는 경우가 있어 리눅스 컨테이너에서 돌린다
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY tests ./tests
COPY pytest.ini .
RUN useradd -m app && mkdir -p /app/data && chown app /app/data
USER app
ENTRYPOINT ["python", "src/main.py"]
