"""
recolector.py
Proceso Recolector: cada 1s lista los PIDs vivos en /proc y los publica en la
Queue de cada analizador. Es el único componente que "sabe" qué procesos
existen en el sistema en un momento dado; los analizadores no listan /proc
por su cuenta para el conjunto de trabajo (aunque sí leen /proc/<pid>/... para
el detalle de cada uno, dato que es privativo de cada analizador).

Usamos Queue(maxsize=1) por analizador: si el analizador todavía no consumió
el mensaje anterior, el Recolector lo descarta y pone el más nuevo (nos
importa el estado actual del sistema, no un historial de listas de PIDs).
"""
import queue
import signal
import procfs


def _publicar(cola, valor):
    try:
        cola.get_nowait()
    except queue.Empty:
        pass
    try:
        cola.put_nowait(valor)
    except queue.Full:
        pass


def correr(colas_por_analizador, shutdown_evt, intervalo=1.0):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while not shutdown_evt.is_set():
        pids = procfs.listar_pids()
        for cola in colas_por_analizador.values():
            _publicar(cola, pids)
        shutdown_evt.wait(intervalo)
