"""
Analizador: Threads / LWPs (vista 4)
Clase 10: threading, GIL, y cómo el kernel ve a cada thread como un LWP con su propio TID en /proc/<pid>/task/<tid>.
"""
import time
import procfs
from analizadores.base import correr_analizador

_prev = {}  # (pid,tid) -> (ticks, ts)


def _cpu_thread(pid, tid, utime, stime):
    clave = (pid, tid)
    ahora = time.time()
    ticks = utime + stime
    anterior = _prev.get(clave)
    _prev[clave] = (ticks, ahora)
    if anterior is None:
        return 0.0
    ticks_prev, t_prev = anterior
    dt = ahora - t_prev
    if dt <= 0:
        return 0.0
    return round(((ticks - ticks_prev) / procfs.CLK_TCK) / dt * 100.0, 1)


def calcular(pids, verbose):
    resultado = {}
    vivos = set()
    for pid in pids:
        tids = procfs.listar_tids(pid)
        if not tids:
            continue
        lista = []
        for tid in tids:
            st = procfs.leer_stat(pid, tid=tid)
            if st is None:
                continue
            status = procfs.leer_status(pid, tid=tid)
            vivos.add((pid, tid))
            lista.append({
                "tid": tid,
                "nombre": procfs.leer_comm_thread(pid, tid),
                "estado": st["state"],
                "cpu_pct": _cpu_thread(pid, tid, st["utime"], st["stime"]),
                "vol_ctx": status.get("voluntary_ctxt_switches", 0) if status else 0,
                "nonvol_ctx": status.get("nonvoluntary_ctxt_switches", 0) if status else 0,
            })
        resultado[pid] = {"cantidad": len(lista), "threads": lista}
    for muerto in [k for k in _prev if k not in vivos]:
        del _prev[muerto]
    return resultado


def correr(cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val):
    correr_analizador("threads", cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val, calcular)
