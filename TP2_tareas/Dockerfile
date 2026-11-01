# Plataforma de tareas distribuidas — Computación II
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY src/ ./src/
COPY tests/ ./tests/
COPY bench/ ./bench/
COPY pytest.ini ./

EXPOSE 8000

# Forma exec (lista): python es el PID 1 y recibe el SIGTERM de
# `docker compose stop` directamente, sin un sh en el medio que lo trague.
CMD ["python", "src/main.py"]
