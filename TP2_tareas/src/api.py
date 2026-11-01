"""
api.py
Endpoints HTTP (FastAPI) y el lifespan que crea/destruye los recursos
compartidos: el pool de workers, el cliente httpx y el ProcessPoolExecutor.

Todos los endpoints son `async def` y ninguno espera a que una tarea
termine: el POST sólo hace put_nowait() en la cola y responde 202.
"""
import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse

import config as configuracion
import persistencia
from modelos import Estado, Pedido, parametros_de
from pool import NoCancelable, Pool, Rechazada
from tareas import EJECUTORES, Contexto

log = logging.getLogger("api")

# Tope de sockets abiertos a la vez por el cliente httpx compartido.
MAX_CONEXIONES_HTTP = 100


@asynccontextmanager
async def lifespan(app):
    cfg = configuracion.cargar()
    persistencia.reportar_volcado_previo(cfg.dir_estado)

    # Procesos para hashear: cada uno tiene su propio intérprete, así que
    # calculan en paralelo de verdad y sin tocar el hilo del event loop.
    procesos = ProcessPoolExecutor(max_workers=cfg.procesos_cpu)

    # Un único AsyncClient para todas las descargas: reutiliza conexiones
    # (keep-alive) y limita los sockets abiertos en total.
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(max_connections=MAX_CONEXIONES_HTTP,
                            max_keepalive_connections=MAX_CONEXIONES_HTTP),
        follow_redirects=True,
    )
    ctx = Contexto(http=http, procesos=procesos)
    pool = Pool(cfg.workers, EJECUTORES, ctx, cola_max=cfg.cola_max)
    pool.iniciar()
    app.state.pool = pool
    app.state.cfg = cfg
    log.info("listo: %d workers, %d procesos CPU, timeout por defecto %gs",
             cfg.workers, cfg.procesos_cpu, cfg.timeout_defecto)
    try:
        yield
    finally:
        await pool.cerrar(cfg.gracia_apagado)
        await persistencia.volcar(pool, cfg.dir_estado)
        await http.aclose()
        # shutdown(wait=True) bloquea hasta que los hijos terminan el bloque
        # que estén haciendo: lo mandamos a un hilo.
        await asyncio.to_thread(procesos.shutdown, True, cancel_futures=True)
        log.info("apagado completo")


app = FastAPI(title="Plataforma de tareas distribuidas", lifespan=lifespan)


def _pool():
    return app.state.pool


@app.post("/tareas", status_code=202)
async def crear_tarea(pedido: Pedido, response: Response):
    pool = _pool()
    timeout = pedido.timeout or app.state.cfg.timeout_defecto
    try:
        tarea = pool.encolar(pedido.tipo, parametros_de(pedido), timeout)
    except Rechazada as e:
        raise HTTPException(status_code=503, detail=str(e))
    response.headers["Location"] = f"/tareas/{tarea.id}"
    return {"id": tarea.id, "estado": tarea.estado.value,
            "timeout": tarea.timeout}


@app.get("/tareas")
async def listar_tareas(estado: Optional[Estado] = None,
                        limite: int = Query(100, ge=1, le=10_000)):
    # Este handler es código síncrono entre dos awaits: mientras corre, el
    # event loop no atiende a nadie más. Con miles de tareas, ordenarlas y
    # serializarlas todas tardaba ~300 ms. Como el dict conserva el orden de
    # inserción (= de creación), lo recorremos al revés y cortamos apenas
    # juntamos `limite`: el costo es O(limite), no O(total).
    # Sin el resultado, que puede ser grande.
    salida = []
    for t in reversed(_pool().tareas.values()):
        if estado is not None and t.estado is not estado:
            continue
        salida.append(t.a_dict(con_resultado=False))
        if len(salida) >= limite:
            break
    return salida


@app.get("/tareas/{tarea_id}")
async def ver_tarea(tarea_id: str):
    tarea = _pool().tareas.get(tarea_id)
    if tarea is None:
        raise HTTPException(status_code=404, detail="no existe esa tarea")
    return tarea.a_dict()


@app.delete("/tareas/{tarea_id}")
async def cancelar_tarea(tarea_id: str):
    try:
        tarea = await _pool().cancelar(tarea_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no existe esa tarea")
    except NoCancelable as e:
        raise HTTPException(status_code=409, detail=str(e))
    return tarea.a_dict(con_resultado=False)


@app.get("/estadisticas")
async def estadisticas():
    return _pool().estadisticas()


# --- bonus -------------------------------------------------------------------

@app.get("/metrics", response_class=PlainTextResponse)
async def metricas():
    """Formato de exposición de texto de Prometheus."""
    est = _pool().estadisticas()
    lineas = ["# TYPE tareas gauge"]
    for estado, n in est["por_estado"].items():
        lineas.append(f'tareas{{estado="{estado}"}} {n}')
    lineas += [
        "# TYPE workers_ocupados gauge",
        f"workers_ocupados {est['workers']['ocupados']}",
        "# TYPE workers_total gauge",
        f"workers_total {est['workers']['total']}",
        "# TYPE cola_pendientes gauge",
        f"cola_pendientes {est['cola']['pendientes']}",
        "# TYPE tarea_duracion_promedio_segundos gauge",
    ]
    for tipo, prom in est["tiempo_promedio_por_tipo"].items():
        lineas.append(f'tarea_duracion_promedio_segundos{{tipo="{tipo}"}} '
                      f'{prom}')
    return "\n".join(lineas) + "\n"
