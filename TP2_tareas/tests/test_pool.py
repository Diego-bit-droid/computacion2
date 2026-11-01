"""
Tests del pool y de las tareas, con pytest-asyncio.

Correr dentro del contenedor:  docker compose run --rm api pytest -v
"""
import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from modelos import Estado
from pool import NoCancelable, Pool, Rechazada
from tareas import EJECUTORES, Contexto


async def _esperar_estado(pool, tarea_id, estados, limite=5.0):
    async with asyncio.timeout(limite):
        while pool.tareas[tarea_id].estado not in estados:
            await asyncio.sleep(0.01)
    return pool.tareas[tarea_id]


def _tareas_vivas():
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


TERMINALES = {Estado.COMPLETADA, Estado.FALLIDA, Estado.CANCELADA}


async def test_esperar_completa(pool):
    t = pool.encolar("esperar", {"segundos": 0.05}, timeout=5)
    assert t.estado is Estado.PENDIENTE   # encolar no espera a la tarea
    t = await _esperar_estado(pool, t.id, TERMINALES)
    assert t.estado is Estado.COMPLETADA
    assert t.resultado["durmio"] >= 0.05


async def test_cancelar_en_ejecucion_no_deja_huerfanas(pool):
    t = pool.encolar("esperar", {"segundos": 30}, timeout=60)
    await _esperar_estado(pool, t.id, {Estado.EJECUTANDO})
    await pool.cancelar(t.id)
    assert t.estado is Estado.CANCELADA
    assert "cliente" in t.error
    assert pool.ocupados == 0
    assert _tareas_vivas() == []
    # el worker sigue vivo y atiende la próxima
    t2 = pool.encolar("esperar", {"segundos": 0}, timeout=5)
    t2 = await _esperar_estado(pool, t2.id, TERMINALES)
    assert t2.estado is Estado.COMPLETADA


async def test_cancelar_pendiente(pool):
    # Ocupamos los 2 workers para que la tercera quede en cola.
    a = pool.encolar("esperar", {"segundos": 30}, timeout=60)
    b = pool.encolar("esperar", {"segundos": 30}, timeout=60)
    c = pool.encolar("esperar", {"segundos": 0}, timeout=60)
    await _esperar_estado(pool, b.id, {Estado.EJECUTANDO})
    await pool.cancelar(c.id)
    assert c.estado is Estado.CANCELADA
    await pool.cancelar(a.id)
    await asyncio.sleep(0.05)      # el worker libre saca a c y la saltea
    assert c.iniciada is None
    await pool.cancelar(b.id)


async def test_cancelar_terminada_da_error(pool):
    t = pool.encolar("esperar", {"segundos": 0}, timeout=5)
    await _esperar_estado(pool, t.id, TERMINALES)
    with pytest.raises(NoCancelable):
        await pool.cancelar(t.id)


async def test_timeout_pasa_a_fallida(pool):
    t = pool.encolar("esperar", {"segundos": 10}, timeout=0.1)
    t = await _esperar_estado(pool, t.id, TERMINALES)
    assert t.estado is Estado.FALLIDA
    assert "timeout" in t.error


async def test_excepcion_no_mata_al_worker(pool):
    t = pool.encolar("esperar", {"segundos": 0, "fallar": True}, timeout=5)
    t = await _esperar_estado(pool, t.id, TERMINALES)
    assert t.estado is Estado.FALLIDA
    assert "RuntimeError" in t.error
    for _ in range(4):   # más tareas que workers: todos siguen vivos
        ok = pool.encolar("esperar", {"segundos": 0}, timeout=5)
        ok = await _esperar_estado(pool, ok.id, TERMINALES)
        assert ok.estado is Estado.COMPLETADA


async def test_cola_llena_rechaza(pool):
    with pytest.raises(Rechazada):
        for _ in range(100):
            pool.encolar("esperar", {"segundos": 30}, timeout=60)


async def test_hashear_coincide_con_hashlib(pool):
    t = pool.encolar("hashear", {"texto": "hola", "iteraciones": 1000},
                     timeout=10)
    t = await _esperar_estado(pool, t.id, TERMINALES)
    h = b"hola"
    for _ in range(1000):
        h = hashlib.sha256(h).digest()
    assert t.resultado["sha256"] == h.hex()


async def test_apagado_cancela_lo_que_no_termina(pool):
    t = pool.encolar("esperar", {"segundos": 30}, timeout=60)
    await _esperar_estado(pool, t.id, {Estado.EJECUTANDO})
    await pool.cerrar(gracia=0.1)
    assert t.estado is Estado.CANCELADA
    assert "apagado" in t.error
    assert _tareas_vivas() == []
    with pytest.raises(Rechazada):
        pool.encolar("esperar", {"segundos": 0}, timeout=5)


# --- descargar con un transporte falso (sin red) -------------------------

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

    def handler(request):
        return httpx.Response(200, stream=_CuerpoLento(registro))

    with ThreadPoolExecutor(1) as ex:
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as http:
            p = Pool(1, EJECUTORES, Contexto(http, ex))
            p.iniciar()
            t = p.encolar("descargar", {"urls": ["http://x/1"]}, timeout=30)
            await asyncio.sleep(0.3)        # ya recibió algunos trozos
            assert t.estado is Estado.EJECUTANDO
            await p.cancelar(t.id)
            assert t.estado is Estado.CANCELADA
            assert registro.get("cerrado") is True
            await p.cerrar(0)


async def test_descargar_concurrente_tarda_lo_que_la_mas_lenta():
    async def handler(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, content=b"ok")

    with ThreadPoolExecutor(1) as ex:
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as http:
            p = Pool(1, EJECUTORES, Contexto(http, ex))
            p.iniciar()
            urls = [f"http://x/{i}" for i in range(10)]
            t = p.encolar("descargar", {"urls": urls}, timeout=30)
            t = await _esperar_estado(p, t.id, TERMINALES)
            assert t.estado is Estado.COMPLETADA
            assert t.resultado["segundos_total"] < 0.6   # no 10 * 0.2
            await p.cerrar(0)


async def test_estadisticas_incrementales_coinciden(pool):
    ids = [pool.encolar("esperar", {"segundos": s}, timeout=t).id
           for s, t in [(0, 5), (0, 5), (30, 60), (10, 0.05), (30, 60)]]
    await asyncio.sleep(0.2)
    await pool.cancelar(ids[2])
    recuento = {e.value: 0 for e in Estado}
    for t in pool.tareas.values():
        recuento[t.estado.value] += 1
    est = pool.estadisticas()
    assert est["por_estado"] == recuento
    assert est["por_estado"]["completada"] == 2
    assert est["tiempo_promedio_por_tipo"]["esperar"] is not None
