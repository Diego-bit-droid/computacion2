"""
Fixtures y helpers compartidos por todos los tests.

Correr dentro del contenedor:  docker compose run --rm --no-deps api pytest -v
"""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from api import app
from modelos import Estado
from pool import Pool
from tareas import EJECUTORES, Contexto

TERMINALES = {Estado.COMPLETADA, Estado.FALLIDA, Estado.CANCELADA}


async def esperar_estado(pool, tarea_id, estados, limite=5.0):
    """Espera (sin bloquear el loop) a que la tarea llegue a uno de `estados`."""
    async with asyncio.timeout(limite):
        while pool.tareas[tarea_id].estado not in estados:
            await asyncio.sleep(0.01)
    return pool.tareas[tarea_id]


def tareas_vivas():
    """Tasks que no son la del test ni los workers del pool."""
    return [t for t in asyncio.all_tasks()
            if t is not asyncio.current_task()
            and not t.get_name().startswith("worker-")]


@pytest.fixture
async def pool():
    # ThreadPoolExecutor en vez de procesos: el test no mide rendimiento,
    # y así no hay que levantar intérpretes.
    with ThreadPoolExecutor(2) as ex:
        async with httpx.AsyncClient() as http:
            p = Pool(2, EJECUTORES, Contexto(http, ex), cola_max=10)
            p.iniciar()
            yield p
            await p.cerrar(gracia=0.1)


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    """La app real, con su lifespan (pool, executor, volcado al cerrar)."""
    monkeypatch.setenv("DIR_ESTADO", str(tmp_path))
    monkeypatch.setenv("WORKERS", "2")
    monkeypatch.setenv("PROCESOS_CPU", "1")
    monkeypatch.setenv("GRACIA_APAGADO", "0.1")
    with TestClient(app) as c:
        yield c


def esperar_estado_http(cliente, tarea_id, estados, limite=5.0):
    """Versión para los tests de la API: hace polling de GET /tareas/{id}.
    Es un test sincrónico (no una corrutina), así que time.sleep acá no
    bloquea ningún event loop."""
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        t = cliente.get(f"/tareas/{tarea_id}").json()
        if t["estado"] in estados:
            return t
        time.sleep(0.02)
    raise AssertionError(f"la tarea {tarea_id} no llegó a {estados}")
