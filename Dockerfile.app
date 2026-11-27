FROM python:3.15.1-slim-bookworm@sha256:575f8bc12ec26542e0678bc6b60e385218248019d433157ec327264a78c3a853
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
