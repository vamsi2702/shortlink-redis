# One image for everything in this project: the API, MiniRedis, the click worker and the database
# migration. docker-compose.yml starts each of them with a different command.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Only the code that runs; tests, docs and notes stay out (see .dockerignore)
COPY pyproject.toml alembic.ini ./
COPY miniredis ./miniredis
COPY api ./api
COPY bench ./bench
RUN pip install .

# Do not run as root inside the container
RUN useradd --create-home app
USER app

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
