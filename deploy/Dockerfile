# syntax=docker/dockerfile:1
# Build context là GỐC repo (compose: build.context = .., dockerfile = deploy/Dockerfile)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Ho_Chi_Minh

# curl: cho HEALTHCHECK. tzdata: để giờ trong container đúng múi giờ VN.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài thư viện trước để tận dụng cache khi chỉ đổi code
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Mã nguồn + cấu hình test (để có thể chạy pytest trong container)
COPY app/ ./app/
COPY tests/ ./tests/
COPY pytest.ini ./

# Chạy bằng user không root. /app/data là volume (SQLite + backup).
# Trên máy chủ: sudo chown -R 10001:10001 data  (nếu log báo "unable to open database file")
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
 && mkdir -p /app/data/backups \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://localhost:8080/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
