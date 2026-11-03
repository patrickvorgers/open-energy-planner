FROM python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83
WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock
COPY src ./src
COPY script/ha_app.py ./script/ha_app.py
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
ENV PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
RUN useradd --uid 10001 --create-home planner && mkdir /data && chown planner /data
CMD ["python", "script/ha_app.py"]
