"""Tests del pool: cola, workers, cancelación, timeouts, apagado y estadísticas."""
import asyncio

import pytest

from conftest import TERMINALES, esperar_estado, tareas_vivas
from modelos import Estado
from pool import NoCancelable, Rechazada


async def test_encolar_no_espera_a_la_tarea(pool):
    t = pool.encolar("esperar", {"segundos": 0.05}, timeout=5)
    assert t.estado is Estado.PENDIENTE
    t = await esperar_estado(pool, t.id, TERMINALES)
    assert t.estado is Estado.COMPLETADA


async def test_cancelar_en_ejecucion_no_deja_huerfanas(pool):
    t = pool.encolar("esperar", {"segundos": 30}, timeout=60)
    await esperar_estado(pool, t.id, {Estado.EJECUTANDO})
    await pool.cancelar(t.id)
    assert t.estado is Estado.CANCELADA
    assert "cliente" in t.error
    assert pool.ocupados == 0
    assert tareas_vivas() == []
    # el worker sigue vivo y atiende la próxima
    t2 = pool.encolar("esperar", {"segundos": 0}, timeout=5)
    t2 = await esperar_estado(pool, t2.id, TERMINALES)
    assert t2.estado is Estado.COMPLETADA


async def test_cancelar_pendiente(pool):
    # Ocupamos los 2 workers para que la tercera quede en cola.
    a = pool.encolar("esperar", {"segundos": 30}, timeout=60)
    b = pool.encolar("esperar", {"segundos": 30}, timeout=60)
    c = pool.encolar("esperar", {"segundos": 0}, timeout=60)
    await esperar_estado(pool, b.id, {Estado.EJECUTANDO})
    await pool.cancelar(c.id)
    assert c.estado is Estado.CANCELADA
    await pool.cancelar(a.id)
    await asyncio.sleep(0.05)      # el worker libre saca a c y la saltea
    assert c.iniciada is None
    await pool.cancelar(b.id)


async def test_cancelar_terminada_da_error(pool):
    t = pool.encolar("esperar", {"segundos": 0}, timeout=5)
    await esperar_estado(pool, t.id, TERMINALES)
    with pytest.raises(NoCancelable):
        await pool.cancelar(t.id)


async def test_timeout_pasa_a_fallida(pool):
    t = pool.encolar("esperar", {"segundos": 10}, timeout=0.1)
    t = await esperar_estado(pool, t.id, TERMINALES)
    assert t.estado is Estado.FALLIDA
    assert "timeout" in t.error


async def test_excepcion_no_mata_al_worker(pool):
    t = pool.encolar("esperar", {"segundos": 0, "fallar": True}, timeout=5)
    t = await esperar_estado(pool, t.id, TERMINALES)
    assert t.estado is Estado.FALLIDA
    assert "RuntimeError" in t.error
    for _ in range(4):   # más tareas que workers: todos siguen vivos
        ok = pool.encolar("esperar", {"segundos": 0}, timeout=5)
        ok = await esperar_estado(pool, ok.id, TERMINALES)
        assert ok.estado is Estado.COMPLETADA


async def test_cola_llena_rechaza(pool):
    with pytest.raises(Rechazada):
        for _ in range(100):
            pool.encolar("esperar", {"segundos": 30}, timeout=60)


async def test_apagado_cancela_lo_que_no_termina(pool):
    t = pool.encolar("esperar", {"segundos": 30}, timeout=60)
    await esperar_estado(pool, t.id, {Estado.EJECUTANDO})
    await pool.cerrar(gracia=0.1)
    assert t.estado is Estado.CANCELADA
    assert "apagado" in t.error
    assert tareas_vivas() == []
    with pytest.raises(Rechazada):
        pool.encolar("esperar", {"segundos": 0}, timeout=5)


async def test_apagado_deja_pendientes_las_que_no_empezaron(pool):
    corriendo = [pool.encolar("esperar", {"segundos": 30}, timeout=60)
                 for _ in range(2)]
    en_cola = pool.encolar("esperar", {"segundos": 0}, timeout=60)
    await esperar_estado(pool, corriendo[1].id, {Estado.EJECUTANDO})
    await pool.cerrar(gracia=0.1)
    assert en_cola.estado is Estado.PENDIENTE
    assert en_cola.iniciada is None


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
