#!/usr/bin/env python3
"""
main.py — Plataforma de tareas distribuidas (TP2 Computación II)

uvicorn atiende SIGTERM y SIGINT: deja de aceptar conexiones, termina las
requests en curso y ejecuta el final del lifespan (api.py), que es donde
esperamos a las tareas en curso y volcamos el estado a JSON.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn

import config as configuracion
from api import app


if __name__ == "__main__":
    # Necesaria por el ProcessPoolExecutor: en macOS/Windows cada proceso
    # hijo re-importa este módulo y, sin la guarda, volvería a levantar el
    # servidor (y otro pool, en bucle).
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: "
                               "%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = configuracion.cargar()
    uvicorn.run(app, host=cfg.host, port=cfg.puerto)
