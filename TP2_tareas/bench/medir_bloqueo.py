#!/usr/bin/env python3
"""
Medición 2 — El event loop bloqueado.

Lanza un hashear pesado y, mientras corre, hace GET /tareas cada 50 ms
midiendo cuánto tarda cada respuesta. Se repite con los dos modos:

  bloqueante : hash_iterado() directo en el event loop (el bug)
  proceso    : run_in_executor con ProcessPoolExecutor (lo correcto)

    docker compose exec api python bench/medir_bloqueo.py [--iteraciones N]
"""
import argparse
import asyncio

from comun import API, TERMINALES, cliente, crear, detalle, reloj


def percentil(valores, p):
    v = sorted(valores)
    return v[min(len(v) - 1, int(len(v) * p))]


async def medir_latencias(c, hasta, cada=0.05):
    """GET /tareas en loop hasta que `hasta()` sea verdadero."""
    lat = []
    while True:
        t0 = reloj()
        await c.get(f"{API}/tareas", params={"limite": 10})
        lat.append((reloj() - t0) * 1000)
        if await hasta():
            return lat
        await asyncio.sleep(cada)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iteraciones", type=int, default=8_000_000)
    args = ap.parse_args()

    async with cliente() as c:
        # Línea de base: sin ninguna tarea corriendo.
        fin = reloj() + 2

        async def dos_segundos():
            return reloj() > fin
        base = await medir_latencias(c, dos_segundos)
        print(f"{'base (sin tarea)':18s} n={len(base):3d}  "
              f"p50={percentil(base, .5):7.1f} ms  "
              f"p95={percentil(base, .95):7.1f} ms  max={max(base):7.1f} ms")

        for modo in ("bloqueante", "proceso"):
            tid = await crear(c, {
                "tipo": "hashear", "texto": "computacion2",
                "iteraciones": args.iteraciones, "modo": modo,
                "timeout": 300})
            estado = {}

            async def termino():
                t = await detalle(c, tid)
                estado.update(t)
                return t["estado"] in TERMINALES

            t0 = reloj()
            lat = await medir_latencias(c, termino)
            total = reloj() - t0
            print(f"{modo:18s} n={len(lat):3d}  "
                  f"p50={percentil(lat, .5):7.1f} ms  "
                  f"p95={percentil(lat, .95):7.1f} ms  "
                  f"max={max(lat):7.1f} ms  "
                  f"(hash: {estado['resultado']['segundos']:.2f}s, "
                  f"ventana medida: {total:.2f}s)")


if __name__ == "__main__":
    asyncio.run(main())
