# Monitor de Procesos y Threads — Computación II
# Sin dependencias externas: la TUI usa curses (stdlib), así que la imagen
# no necesita red para levantar (pip install no descarga nada obligatorio).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TERM=xterm-256color

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.json .
COPY src/ ./src/

ENTRYPOINT ["python3", "src/main.py"]
