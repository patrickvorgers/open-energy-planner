FROM python:3.15.2-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9
WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock
COPY src ./src
COPY script ./script
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
ENV PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
RUN useradd --uid 10001 --create-home planner && mkdir /data && chown planner /data
USER planner
CMD ["python", "-m", "open_energy_planner.app", "--demo", "--host", "0.0.0.0"]
