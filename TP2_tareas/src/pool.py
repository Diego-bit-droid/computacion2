"""
pool.py
Cola de tareas + pool de N workers (corrutinas) que la consumen.

Productor-consumidor sobre una asyncio.Queue: el POST produce (put_nowait,
nunca espera) y cada worker consume (await get, se suspende si no hay nada).

Cada tarea corre en su PROPIA asyncio.Task, separada de la del worker:
  - DELETE cancela esa Task sin matar al worker, que sigue con la próxima.
  - asyncio.timeout() la corta al vencer el plazo.
  - Si el worker mismo es cancelado (apagado), al estar esperando con
    `await handle` la cancelación se propaga a la tarea hija.

No hay ningún Lock: todo esto corre en el hilo del event loop y una corrutina
sólo puede ser interrumpida en un `await`. Los cambios de estado (pendiente ->
ejecutando, contadores, el dict de tareas) no tienen await en el medio, así
que no hay forma de que otra corrutina vea un estado a medio escribir.
"""
import asyncio
import logging
import time

from modelos import Estado, Tarea

log = logging.getLogger("pool")


class Rechazada(Exception):
    """El pool no acepta la tarea (apagándose o cola llena)."""


class NoCancelable(Exception):
    """La tarea ya terminó."""


class Pool:
    def __init__(self, workers, ejecutores, ctx, cola_max=1000):
        self.n_workers = workers
        self.ejecutores = ejecutores
        self.ctx = ctx
        # Acotada a propósito: con 10.000 POST de golpe preferimos responder
        # 503 a partir de cola_max que crecer en memoria sin límite.
        self.cola = asyncio.Queue(maxsize=cola_max)
        self.tareas = {}          # id -> Tarea (todas, incluidas terminadas)
        self._en_curso = {}       # id -> asyncio.Task de la tarea que corre
        self._workers = []
        self.aceptando = True
        self.ocupados = 0
        self.inicio = time.time()
        # Estadísticas incrementales: se actualizan en cada cambio de estado
        # para que GET /estadisticas sea O(1) y no recorra todas las tareas
        # (con miles de tareas, ese recorrido frenaba el event loop).
        self._por_estado = {e: 0 for e in Estado}
        self._duraciones = {}         # tipo -> [suma, cantidad] (completadas)
        self._espera = [0.0, 0]       # tiempo en cola: [suma, cantidad]

    # --- ciclo de vida -------------------------------------------------------

    def iniciar(self):
        for n in range(self.n_workers):
            self._lanzar_worker(n)
        log.info("pool iniciado con %d workers", self.n_workers)

    def _lanzar_worker(self, n):
        w = asyncio.create_task(self._worker(n), name=f"worker-{n}")
        w.add_done_callback(lambda t, n=n: self._worker_murio(t, n))
        self._workers.append(w)

    def _worker_murio(self, t, n):
        # Red de seguridad: _ejecutar ya atrapa todas las excepciones de las
        # tareas, así que esto sólo pasa por un bug del propio worker. Lo
        # logueamos y lo reemplazamos para no perder capacidad en silencio.
        self._workers = [w for w in self._workers if w is not t]
        if t.cancelled() or not self.aceptando:
            return
        log.error("worker-%d murió: %r; relanzando", n, t.exception())
        self._lanzar_worker(n)

    async def cerrar(self, gracia):
        """Apagado ordenado: no aceptar más, esperar a las que corren (como
        mucho `gracia` segundos), cancelar las que queden."""
        self.aceptando = False
        en_curso = list(self._en_curso.values())
        if en_curso:
            log.info("esperando %d tareas en curso (máx %.1fs)",
                     len(en_curso), gracia)
            _, pendientes = await asyncio.wait(en_curso, timeout=gracia)
            if pendientes:
                log.warning("%d tareas no terminaron a tiempo: se cancelan",
                            len(pendientes))
        # Cancelar a los workers propaga la cancelación a la tarea que cada
        # uno esté esperando. gather(..., return_exceptions=True) espera a
        # que TODOS terminen de verdad: no quedan corrutinas huérfanas.
        workers = list(self._workers)
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        log.info("pool cerrado")

    # --- API que usan los endpoints -----------------------------------------

    def encolar(self, tipo, parametros, timeout):
        if not self.aceptando:
            raise Rechazada("el servidor se está apagando")
        tarea = Tarea(tipo=tipo, parametros=parametros, timeout=timeout)
        try:
            self.cola.put_nowait(tarea.id)
        except asyncio.QueueFull:
            raise Rechazada(f"cola llena ({self.cola.maxsize} pendientes)")
        self.tareas[tarea.id] = tarea
        self._por_estado[Estado.PENDIENTE] += 1
        return tarea

    async def cancelar(self, tarea_id):
        tarea = self.tareas[tarea_id]          # KeyError -> 404 en la API
        if tarea.estado.terminal:
            raise NoCancelable(f"la tarea ya está {tarea.estado.value}")
        tarea.cancelada_por_cliente = True
        if tarea.estado is Estado.PENDIENTE:
            # Sigue en la cola: no la sacamos (asyncio.Queue no lo permite),
            # el worker que la saque la va a ver cancelada y la salteará.
            self._finalizar(tarea, Estado.CANCELADA, "cancelada estando en cola")
            return tarea
        handle = self._en_curso.get(tarea_id)
        if handle is not None:
            handle.cancel()
            # Esperamos (poco) a que la cancelación se complete para que la
            # respuesta del DELETE ya muestre "cancelada". asyncio.wait no
            # re-cancela ni propaga: si tarda más, se responde "ejecutando"
            # y el estado final llega solo un instante después.
            await asyncio.wait({handle}, timeout=2.0)
        return tarea

    def estadisticas(self):
        suma = sum(s for s, _ in self._duraciones.values())
        cant = sum(n for _, n in self._duraciones.values())
        return {
            "total": len(self.tareas),
            "por_estado": {e.value: n for e, n in self._por_estado.items()},
            "workers": {"total": self.n_workers, "ocupados": self.ocupados},
            "cola": {"pendientes": self.cola.qsize(),
                     "capacidad": self.cola.maxsize},
            "tiempo_promedio_ejecucion": _promedio(suma, cant),
            "tiempo_promedio_por_tipo": {k: _promedio(s, n) for k, (s, n)
                                         in self._duraciones.items()},
            "tiempo_promedio_en_cola": _promedio(*self._espera),
            "aceptando": self.aceptando,
            "uptime": round(time.time() - self.inicio, 1),
        }

    # --- workers -------------------------------------------------------------

    async def _worker(self, n):
        while True:
            tarea_id = await self.cola.get()
            try:
                tarea = self.tareas.get(tarea_id)
                if tarea is None or tarea.estado is not Estado.PENDIENTE:
                    continue        # cancelada mientras esperaba en la cola
                if not self.aceptando:
                    continue        # apagando: queda "pendiente" en el volcado
                await self._ejecutar(tarea, n)
            finally:
                self.cola.task_done()

    async def _ejecutar(self, tarea, n):
        self._cambiar_estado(tarea, Estado.EJECUTANDO)
        tarea.iniciada = time.time()
        tarea.worker = n
        self._espera[0] += tarea.iniciada - tarea.creada
        self._espera[1] += 1
        self.ocupados += 1

        handle = asyncio.create_task(self._correr(tarea),
                                     name=f"tarea-{tarea.id}")
        self._en_curso[tarea.id] = handle
        try:
            tarea.resultado = await handle
            self._finalizar(tarea, Estado.COMPLETADA)
        except asyncio.CancelledError:
            # ¿Cancelaron la tarea (DELETE) o me cancelaron a mí (apagado)?
            if tarea.cancelada_por_cliente:
                # Era para la tarea: la anoto y el worker sigue vivo.
                self._finalizar(tarea, Estado.CANCELADA,
                                "cancelada por el cliente")
            else:
                # Era para el worker: anoto y re-lanzo para que termine.
                self._finalizar(tarea, Estado.CANCELADA,
                                "cancelada por apagado del servidor")
                raise
        except TimeoutError:
            self._finalizar(tarea, Estado.FALLIDA,
                            f"timeout: superó {tarea.timeout:g}s")
        except Exception as e:
            # Cualquier excepción de la tarea queda en ella; el worker sigue.
            log.warning("tarea %s falló: %r", tarea.id, e)
            self._finalizar(tarea, Estado.FALLIDA, f"{type(e).__name__}: {e}")
        finally:
            del self._en_curso[tarea.id]
            self.ocupados -= 1

    async def _correr(self, tarea):
        ejecutor = self.ejecutores[tarea.tipo]
        async with asyncio.timeout(tarea.timeout):
            return await ejecutor(tarea, self.ctx)

    def _cambiar_estado(self, tarea, estado):
        self._por_estado[tarea.estado] -= 1
        self._por_estado[estado] += 1
        tarea.estado = estado

    def _finalizar(self, tarea, estado, error=None):
        self._cambiar_estado(tarea, estado)
        tarea.error = error
        tarea.finalizada = time.time()
        if estado is Estado.COMPLETADA and tarea.iniciada is not None:
            acum = self._duraciones.setdefault(tarea.tipo, [0.0, 0])
            acum[0] += tarea.finalizada - tarea.iniciada
            acum[1] += 1


def _promedio(suma, cantidad):
    return round(suma / cantidad, 4) if cantidad else None
