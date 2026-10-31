"""
config.py
Configuración por variables de entorno (las setea docker-compose.yml).

Todo se lee una sola vez al arrancar; el resto del código recibe un Config
inmutable y no vuelve a tocar os.environ.
"""
import os
from dataclasses import dataclass


def _env_int(nombre, defecto, minimo=1):
    try:
        valor = int(os.environ.get(nombre, defecto))
    except ValueError:
        valor = defecto
    return max(minimo, valor)


def _env_float(nombre, defecto, minimo=0.0):
    try:
        valor = float(os.environ.get(nombre, defecto))
    except ValueError:
        valor = defecto
    return max(minimo, valor)


@dataclass(frozen=True)
class Config:
    host: str
    puerto: int
    workers: int               # corrutinas consumiendo la asyncio.Queue
    procesos_cpu: int          # procesos del ProcessPoolExecutor (hashear)
    cola_max: int              # tope de tareas pendientes (backpressure)
    timeout_defecto: float     # segundos, si el POST no manda uno
    gracia_apagado: float      # cuánto esperamos a las tareas en curso al apagar
    dir_estado: str            # dónde van los estado_<ts>.json


def cargar():
    return Config(
        host=os.environ.get("HOST", "0.0.0.0"),
        puerto=_env_int("PUERTO", 8000),
        workers=_env_int("WORKERS", 4),
        procesos_cpu=_env_int("PROCESOS_CPU", os.cpu_count() or 2),
        cola_max=_env_int("COLA_MAX", 1000),
        timeout_defecto=_env_float("TIMEOUT_DEFECTO", 60.0, minimo=0.1),
        gracia_apagado=_env_float("GRACIA_APAGADO", 10.0),
        dir_estado=os.environ.get("DIR_ESTADO", "data"),
    )
