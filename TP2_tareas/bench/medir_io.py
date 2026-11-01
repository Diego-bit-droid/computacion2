#!/usr/bin/env python3
"""
Medición 1 — Concurrencia I/O: 20 descargas secuenciales vs 20 concurrentes.

Usa el servidor_lento (demoras conocidas entre 0.2 y 0.8 s por url) así el
resultado no depende de la red. Se corre dentro del contenedor:

    docker compose exec api python bench/medir_io.py [--urls 20] [--rep 3]

Con --externas usa un sitio real en vez del servidor lento.
"""
import argparse
import asyncio
import statistics

from comun import LENTO, cliente, crear, detalle, esperar_todas


async def una_corrida(c, urls, modo):
    tid = await crear(c, {"tipo": "descargar", "urls": urls, "modo": modo})
    await esperar_todas(c, [tid])
    t = await detalle(c, tid)
    assert t["estado"] == "completada", t
    return t["resultado"]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", type=int, default=20)
    ap.add_argument("--rep", type=int, default=3)
    ap.add_argument("--externas", action="store_true")
    args = ap.parse_args()

    if args.externas:
        urls = [f"https://httpbin.org/delay/{1 + i % 2}"
                for i in range(args.urls)]
    else:
        urls = [f"{LENTO}/demora/{i}" for i in range(args.urls)]

    async with cliente() as c:
        tiempos = {"secuencial": [], "concurrente": []}
        for rep in range(args.rep):
            for modo in ("secuencial", "concurrente"):
                r = await una_corrida(c, urls, modo)
                tiempos[modo].append(r["segundos_total"])
                print(f"rep {rep + 1} {modo:12s} total={r['segundos_total']:.3f}s"
                      f"  suma individual={r['suma_individual']:.3f}s"
                      f"  más lenta={r['mas_lenta']:.3f}s"
                      f"  errores={r['errores']}")
        sec = statistics.mean(tiempos["secuencial"])
        con = statistics.mean(tiempos["concurrente"])
        print(f"\n{args.urls} descargas, promedio de {args.rep} corridas:")
        print(f"  secuencial  : {sec:.3f} s")
        print(f"  concurrente : {con:.3f} s")
        print(f"  speedup     : {sec / con:.1f}x")


if __name__ == "__main__":
    asyncio.run(main())
