#!/usr/bin/env python3
"""
servidor_lento.py
Servidor HTTP de prueba para las mediciones: responde con una demora
conocida. Así las mediciones no dependen de la red ni de un sitio externo y
se pueden repetir con los mismos números.

  GET /demora/{n}          demora determinística entre 0.2 y 0.8 s según n
  GET /lento?ms=500&kb=64  demora y tamaño explícitos
"""
import asyncio

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import Response

app = FastAPI(title="servidor lento")


def demora_de(n):
    # 0.2 .. 0.8 s, distinta para cada n pero siempre la misma para el mismo n
    return 0.2 + ((n * 7919) % 601) / 1000


@app.get("/demora/{n}")
async def demora(n: int, kb: int = Query(16, ge=0, le=10_240)):
    await asyncio.sleep(demora_de(n))
    return Response(b"x" * (kb * 1024), media_type="application/octet-stream")


@app.get("/lento")
async def lento(ms: int = Query(500, ge=0, le=600_000),
                kb: int = Query(16, ge=0, le=10_240)):
    await asyncio.sleep(ms / 1000)
    return Response(b"x" * (kb * 1024), media_type="application/octet-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")
