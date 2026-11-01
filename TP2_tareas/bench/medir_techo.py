#!/usr/bin/env python3
"""
Medición 3 — Techo del pool.

Manda K tareas iguales de golpe, para K creciente, y mide el tiempo total
hasta que terminan todas, el tiempo promedio en cola y el de ejecución.

    docker compose exec api python bench/medir_techo.py --tipo esperar
    docker compose exec api python bench/medir_techo.py --tipo hashear
    docker compose exec api python bench/medir_techo.py --tipo descargar

Conviene repetirlo cambiando WORKERS / PROCESOS_CPU:
    WORKERS=64 docker compose up -d --build
"""
import argparse
import asyncio
import statistics

from comun import API, LENTO, cliente, crear, esperar_todas

CUERPOS = {
    "esperar": lambda i: {"tipo": "esperar", "segundos": 1},
    "hashear": lambda i: {"tipo": "hashear", "texto": f"t{i}",
                          "iteraciones": 2_000_000},
    # Cada tarea descarga 1 url que tarda 1 s en el servidor lento.
    "descargar": lambda i: {"tipo": "descargar",
                            "urls": [f"{LENTO}/lento?ms=1000&kb=1"]},
}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tipo", choices=CUERPOS, default="esperar")
    ap.add_argument("--cargas", default="1,2,4,8,16,32,64")
    args = ap.parse_args()
    cargas = [int(x) for x in args.cargas.split(",")]

    async with cliente() as c:
        est = (await c.get(f"{API}/estadisticas")).json()
    print(f"tipo={args.tipo}  workers={est['workers']['total']}")
    print(f"{'K':>5} {'total(s)':>9} {'ejec. prom(s)':>14} "
          f"{'ejec. max(s)':>13} {'en cola prom(s)':>16} {'tareas/s':>9}")
    for k in cargas:
        async with cliente(conexiones=200) as c:
            ids = await asyncio.gather(*(crear(c, CUERPOS[args.tipo](i))
                                         for i in range(k)))
            tareas = await esperar_todas(c, ids)
            # Tiempos del servidor: desde que se creó la primera hasta que
            # terminó la última. No depende de lo que tarde este script.
            total = (max(t["finalizada"] for t in tareas)
                     - min(t["creada"] for t in tareas))
            ejec = [t["duracion"] for t in tareas]
            cola = statistics.mean(t["iniciada"] - t["creada"] for t in tareas)
            malas = [t for t in tareas if t["estado"] != "completada"]
            print(f"{k:5d} {total:9.2f} {statistics.mean(ejec):14.2f} "
                  f"{max(ejec):13.2f} {cola:16.2f} {k / total:9.1f}"
                  + (f"  ({len(malas)} no completadas)" if malas else ""))


if __name__ == "__main__":
    asyncio.run(main())
