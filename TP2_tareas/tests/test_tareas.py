"""Tests de las tres tareas. descargar usa un transporte falso de httpx (sin red)."""
import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor

import httpx

from conftest import TERMINALES, esperar_estado
from modelos import Estado
from pool import Pool
from tareas import EJECUTORES, Contexto


async def _pool_con_transporte(handler):
    """Pool de 1 worker cuyo cliente httpx responde con `handler`."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ex = ThreadPoolExecutor(1)
    p = Pool(1, EJECUTORES, Contexto(http, ex))
    p.iniciar()
    return p, http, ex


async def _cerrar(p, http, ex):
    await p.cerrar(0)
    await http.aclose()
    ex.shutdown()


# --- esperar ------------------------------------------------------------------

async def test_esperar_duerme_lo_pedido(pool):
    t = pool.encolar("esperar", {"segundos": 0.1}, timeout=5)
    t = await esperar_estado(pool, t.id, TERMINALES)
    assert t.estado is Estado.COMPLETADA
    assert t.resultado["durmio"] >= 0.1


# --- hashear ------------------------------------------------------------------

async def test_hashear_coincide_con_hashlib(pool):
    t = pool.encolar("hashear", {"texto": "hola", "iteraciones": 1000},
                     timeout=10)
    t = await esperar_estado(pool, t.id, TERMINALES)
    h = b"hola"
    for _ in range(1000):
        h = hashlib.sha256(h).digest()
    assert t.resultado["sha256"] == h.hex()


# --- descargar ----------------------------------------------------------------

async def test_descargar_concurrente_tarda_lo_que_la_mas_lenta():
    async def handler(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, content=b"ok")

    p, http, ex = await _pool_con_transporte(handler)
    urls = [f"http://x/{i}" for i in range(10)]
    t = p.encolar("descargar", {"urls": urls}, timeout=30)
    t = await esperar_estado(p, t.id, TERMINALES)
    assert t.estado is Estado.COMPLETADA
    assert t.resultado["segundos_total"] < 0.6   # no 10 * 0.2
    await _cerrar(p, http, ex)


async def test_descargar_secuencial_tarda_la_suma():
    async def handler(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, content=b"ok")

    p, http, ex = await _pool_con_transporte(handler)
    urls = [f"http://x/{i}" for i in range(5)]
    t = p.encolar("descargar", {"urls": urls, "modo": "secuencial"},
                  timeout=30)
    t = await esperar_estado(p, t.id, TERMINALES)
    assert t.resultado["segundos_total"] >= 0.5
    await _cerrar(p, http, ex)


async def test_descargar_un_error_no_tira_las_demas():
    def handler(request):
        if request.url.path == "/mala":
            raise httpx.ConnectError("sin conexión", request=request)
        return httpx.Response(200, content=b"x" * 10)

    p, http, ex = await _pool_con_transporte(handler)
    t = p.encolar("descargar", {"urls": ["http://x/buena", "http://x/mala"]},
                  timeout=30)
    t = await esperar_estado(p, t.id, TERMINALES)
    assert t.estado is Estado.COMPLETADA
    buena, mala = t.resultado["urls"]
    assert buena["estado_http"] == 200 and buena["bytes"] == 10
    assert "ConnectError" in mala["error"]
    assert t.resultado["errores"] == 1
    await _cerrar(p, http, ex)


async def test_descargar_falla_si_fallan_todas():
    def handler(request):
        raise httpx.ConnectError("sin conexión", request=request)

    p, http, ex = await _pool_con_transporte(handler)
    t = p.encolar("descargar", {"urls": ["http://x/1", "http://x/2"]},
                  timeout=30)
    t = await esperar_estado(p, t.id, TERMINALES)
    assert t.estado is Estado.FALLIDA
    await _cerrar(p, http, ex)


class _CuerpoLento(httpx.AsyncByteStream):
    def __init__(self, registro):
        self.registro = registro

    async def __aiter__(self):
        for _ in range(100):
            await asyncio.sleep(0.05)
            yield b"x" * 1024

    async def aclose(self):
        self.registro["cerrado"] = True


async def test_cancelar_a_mitad_de_descarga_cierra_la_respuesta():
    registro = {}
    p, http, ex = await _pool_con_transporte(
        lambda request: httpx.Response(200, stream=_CuerpoLento(registro)))
    t = p.encolar("descargar", {"urls": ["http://x/1"]}, timeout=30)
    await asyncio.sleep(0.3)        # ya recibió algunos trozos
    assert t.estado is Estado.EJECUTANDO
    await p.cancelar(t.id)
    assert t.estado is Estado.CANCELADA
    assert registro.get("cerrado") is True
    await _cerrar(p, http, ex)
