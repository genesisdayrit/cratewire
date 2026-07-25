# SpotWire server — FastAPI + spotdl + ffmpeg, reproducible for local + Hetzner.
FROM python:3.12-slim

# ffmpeg is a SYSTEM binary spotdl shells out to — it is NOT a pip/uv dep.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# uv: fast, reproducible installs from the committed lockfile.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install deps first (cached layer) using the lockfile, without the app code.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now the app.
COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
