"""Helpers compartidos por los scripts de medición."""
import asyncio
import os
import time

import httpx

API = os.environ.get("API", "http://localhost:8000")
LENTO = os.environ.get("LENTO", "http://lento:8001")
TERMINALES = {"completada", "fallida", "cancelada"}


def cliente(conexiones=100, timeout=600):
    return httpx.AsyncClient(
        timeout=timeout, limits=httpx.Limits(max_connections=conexiones))


async def crear(c, cuerpo):
    r = await c.post(f"{API}/tareas", json=cuerpo)
    r.raise_for_status()
    return r.json()["id"]


async def esperar_todas(c, ids, cada=0.1):
    """Espera a que no quede nada pendiente ni ejecutando y devuelve los
    dicts (sin resultado) de esas tareas. Supone que el bench es lo único
    que está mandando tareas."""
    while True:
        await asyncio.sleep(cada)
        est = (await c.get(f"{API}/estadisticas")).json()["por_estado"]
        if est["pendiente"] == 0 and est["ejecutando"] == 0:
            break
    lista = (await c.get(f"{API}/tareas",
                         params={"limite": max(len(ids), 1)})).json()
    por_id = {t["id"]: t for t in lista}
    return [por_id[i] for i in ids]


async def detalle(c, tarea_id):
    return (await c.get(f"{API}/tareas/{tarea_id}")).json()


def reloj():
    return time.perf_counter()
