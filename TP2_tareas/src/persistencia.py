"""
persistencia.py
Volcado del estado a estado_<timestamp>.json al apagar, y aviso por log al
arrancar si quedó un volcado previo.

Escribir un archivo es I/O de disco bloqueante y asyncio no tiene I/O de
archivos asíncrono, así que el volcado corre en un hilo (asyncio.to_thread)
para no frenar el event loop mientras se serializa todo.
"""
import asyncio
import glob
import json
import logging
import os
import time

log = logging.getLogger("persistencia")


def _escribir(ruta, datos):
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)


async def volcar(pool, directorio):
    os.makedirs(directorio, exist_ok=True)
    ahora = time.time()
    ruta = os.path.join(directorio, f"estado_{int(ahora)}.json")
    datos = {
        "volcado": ahora,
        "estadisticas": pool.estadisticas(),
        "tareas": [t.a_dict() for t in pool.tareas.values()],
    }
    await asyncio.to_thread(_escribir, ruta, datos)
    log.info("estado volcado en %s (%d tareas)", ruta, len(datos["tareas"]))
    return ruta


def reportar_volcado_previo(directorio):
    volcados = sorted(glob.glob(os.path.join(directorio, "estado_*.json")),
                      key=os.path.getmtime)
    if not volcados:
        log.info("no hay volcados previos en %s/", directorio)
        return None
    ultimo = volcados[-1]
    try:
        with open(ultimo, encoding="utf-8") as f:
            datos = json.load(f)
        por_estado = {}
        for t in datos.get("tareas", []):
            por_estado[t["estado"]] = por_estado.get(t["estado"], 0) + 1
        log.info("volcado previo encontrado: %s (%d tareas: %s; %d volcados "
                 "en total)", ultimo, len(datos.get("tareas", [])),
                 por_estado or "ninguna", len(volcados))
    except (OSError, ValueError) as e:
        log.warning("volcado previo %s ilegible: %s", ultimo, e)
    return ultimo
