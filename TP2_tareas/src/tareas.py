"""
tareas.py
Implementación de los tres tipos de tarea. Cada uno es una corrutina
`async def tipo(tarea, ctx) -> dict` que devuelve el resultado.

Ninguna de estas corrutinas maneja timeouts ni cancelación por su cuenta: el
pool las envuelve en `asyncio.timeout()` y las corre en una Task propia que el
DELETE puede cancelar. Lo único que tienen que garantizar es que la
CancelledError se propague (nunca la tragamos: `except Exception` no la
atrapa porque desde 3.8 hereda de BaseException) y que los recursos se
liberen con `async with` / `finally`.
"""
import asyncio
import hashlib
import time
from dataclasses import dataclass
from concurrent.futures import Executor

import httpx

# Iteraciones de SHA-256 por bloque mandado al executor. Con ~0.8-1 M hashes/s
# por núcleo, un bloque tarda ~0.12 s: es el peor caso de demora entre que se
# pide una cancelación y que el proceso hijo deja de trabajar en esa tarea.
BLOQUE_HASH = 100_000

# Máximo de descargas simultáneas dentro de UNA tarea (clase 20, "Limitar la
# concurrencia"): una tarea con 500 urls no abre 500 conexiones a la vez.
DESCARGAS_SIMULTANEAS = 20


@dataclass
class Contexto:
    """Recursos compartidos que las tareas necesitan (se crean en el lifespan)."""
    http: httpx.AsyncClient
    procesos: Executor


# --- descargar (I/O-bound) --------------------------------------------------

async def _descargar_una(http, url):
    t0 = time.perf_counter()
    try:
        # stream() + aiter_bytes(): contamos el tamaño sin cargar el cuerpo
        # entero en memoria. El `async with` cierra la respuesta (y devuelve
        # o cierra el socket) pase lo que pase, incluso si nos cancelan a
        # mitad del cuerpo: la CancelledError sale del `async for` y el
        # __aexit__ corre igual.
        async with http.stream("GET", url) as resp:
            tamanio = 0
            async for trozo in resp.aiter_bytes():
                tamanio += len(trozo)
        return {
            "url": url,
            "estado_http": resp.status_code,
            "bytes": tamanio,
            "segundos": round(time.perf_counter() - t0, 4),
        }
    except httpx.HTTPError as e:
        # Un error de red en UNA url no tiene que tirar la tarea entera.
        return {
            "url": url,
            "error": f"{type(e).__name__}: {e}",
            "segundos": round(time.perf_counter() - t0, 4),
        }


async def descargar(tarea, ctx):
    urls = tarea.parametros["urls"]
    modo = tarea.parametros.get("modo", "concurrente")
    limite = asyncio.Semaphore(DESCARGAS_SIMULTANEAS)
    hechas = 0

    async def una(url):
        nonlocal hechas
        async with limite:
            r = await _descargar_una(ctx.http, url)
        hechas += 1
        tarea.progreso = hechas / len(urls)
        return r

    t0 = time.perf_counter()
    if modo == "secuencial":
        # Un for con await adentro: la siguiente descarga no empieza hasta
        # que termina la anterior. Tiempo total = suma de todas.
        resultados = [await una(u) for u in urls]
    else:
        # TaskGroup (3.11) en vez de gather: si una corrutina hija lanzara
        # una excepción, el TaskGroup cancela a las hermanas y espera a que
        # terminen; gather sin return_exceptions las dejaría huérfanas.
        # Si cancelan la tarea, el TaskGroup cancela a todas las hijas.
        async with asyncio.TaskGroup() as tg:
            hijas = [tg.create_task(una(u)) for u in urls]
        resultados = [h.result() for h in hijas]
    total = time.perf_counter() - t0

    ok = [r for r in resultados if "error" not in r]
    if not ok:
        raise RuntimeError(f"fallaron las {len(urls)} descargas")
    return {
        "modo": modo,
        "segundos_total": round(total, 4),
        "suma_individual": round(sum(r["segundos"] for r in resultados), 4),
        "mas_lenta": round(max(r["segundos"] for r in resultados), 4),
        "bytes_total": sum(r.get("bytes", 0) for r in resultados),
        "errores": len(resultados) - len(ok),
        "urls": resultados,
    }


# --- hashear (CPU-bound) ----------------------------------------------------

def hash_iterado(datos, iteraciones):
    """Función pura y top-level: tiene que poder picklearse para viajar al
    proceso hijo del ProcessPoolExecutor."""
    h = datos
    for _ in range(iteraciones):
        h = hashlib.sha256(h).digest()
    return h


async def hashear(tarea, ctx):
    texto = tarea.parametros["texto"]
    total = tarea.parametros["iteraciones"]
    modo = tarea.parametros.get("modo", "proceso")
    loop = asyncio.get_running_loop()

    h = texto.encode()
    hechas = 0
    t0 = time.perf_counter()
    # Partimos el trabajo en bloques: entre bloque y bloque hay un `await`,
    # que es el punto donde la cancelación o el timeout pueden actuar. Un
    # future de un proceso hijo que ya está corriendo NO se puede
    # interrumpir, así que sin bloques una tarea "cancelada" seguiría
    # ocupando un núcleo hasta el final.
    while hechas < total:
        n = min(BLOQUE_HASH, total - hechas)
        if modo == "proceso":
            h = await loop.run_in_executor(ctx.procesos, hash_iterado, h, n)
        else:
            # "bloqueante": EL BUG, a propósito. Corre en el hilo del event
            # loop, sin await en el medio: mientras dura nadie más avanza
            # (ni el GET /tareas, ni los timeouts, ni las otras tareas).
            h = hash_iterado(h, n)
        hechas += n
        tarea.progreso = hechas / total
    return {
        "modo": modo,
        "iteraciones": total,
        "sha256": h.hex(),
        "segundos": round(time.perf_counter() - t0, 4),
    }


# --- esperar (I/O simulado) -------------------------------------------------

async def esperar(tarea, ctx):
    segundos = tarea.parametros["segundos"]
    t0 = time.perf_counter()
    # asyncio.sleep, nunca time.sleep: asyncio.sleep suspende sólo esta
    # corrutina (registra un timer en el loop y cede el control), time.sleep
    # dormiría el hilo entero y con él a todas las demás corrutinas.
    fin = t0 + segundos
    while (restante := fin - time.perf_counter()) > 0:
        await asyncio.sleep(min(restante, 0.25))
        tarea.progreso = min(1.0, (time.perf_counter() - t0) / segundos)
    tarea.progreso = 1.0
    if tarea.parametros.get("fallar"):
        raise RuntimeError("falla pedida por el cliente (fallar=true)")
    return {"durmio": round(time.perf_counter() - t0, 4)}


EJECUTORES = {
    "descargar": descargar,
    "hashear": hashear,
    "esperar": esperar,
}
