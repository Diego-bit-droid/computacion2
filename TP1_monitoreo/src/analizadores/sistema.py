"""
Analizador: Sistema global (vista 7)
Clase 3-4: estados de proceso a nivel sistema (zombies = terminados sin wait() del padre).

Este analizador también escribe en el Array compartido `cpu_quick` (4 doubles:
user%, system%, idle%, iowait%) además del Manager dict. Es una demostración
deliberada de cuándo conviene Array en vez de Manager: son 4 escalares que
cambian todo el tiempo y que el Display quiere leer sin pagar el costo de una
llamada RPC al proceso servidor del Manager por cada refresco.
"""
import time
import procfs
from analizadores.base import correr_analizador

_prev_cpu_global = None  # (dict de campos, timestamp)
_prev_por_pid = {}  # pid -> (ticks, ts) -- cache propio, independiente del de resumen.py


def _cpu_global_pct():
    global _prev_cpu_global
    actual = procfs.leer_cpu_global()
    ahora = time.time()
    if actual is None:
        return {"user": 0.0, "system": 0.0, "idle": 0.0, "iowait": 0.0}
    if _prev_cpu_global is None:
        _prev_cpu_global = (actual, ahora)
        return {"user": 0.0, "system": 0.0, "idle": 0.0, "iowait": 0.0}
    anterior, _ = _prev_cpu_global
    _prev_cpu_global = (actual, ahora)
    deltas = {k: actual[k] - anterior.get(k, 0) for k in actual}
    total = sum(deltas.values())
    if total <= 0:
        return {"user": 0.0, "system": 0.0, "idle": 0.0, "iowait": 0.0}
    return {
        "user": round((deltas["user"] + deltas["nice"]) / total * 100, 1),
        "system": round((deltas["system"] + deltas["irq"] + deltas["softirq"]) / total * 100, 1),
        "idle": round(deltas["idle"] / total * 100, 1),
        "iowait": round(deltas["iowait"] / total * 100, 1),
    }


def _cpu_pct_pid(pid, utime, stime):
    ahora = time.time()
    ticks = utime + stime
    anterior = _prev_por_pid.get(pid)
    _prev_por_pid[pid] = (ticks, ahora)
    if anterior is None:
        return 0.0
    ticks_prev, t_prev = anterior
    dt = ahora - t_prev
    if dt <= 0:
        return 0.0
    return round(((ticks - ticks_prev) / procfs.CLK_TCK) / dt * 100.0, 1)


def calcular(pids, verbose, cpu_quick_array):
    cpu_pct = _cpu_global_pct()
    cpu_quick_array[0] = cpu_pct["user"]
    cpu_quick_array[1] = cpu_pct["system"]
    cpu_quick_array[2] = cpu_pct["idle"]
    cpu_quick_array[3] = cpu_pct["iowait"]

    conteo_estados = {}
    total_threads = 0
    zombies = 0
    top_cpu = []
    top_mem = []

    for pid in pids:
        st = procfs.leer_stat(pid)
        if st is None:
            continue
        estado = st["state"]
        conteo_estados[estado] = conteo_estados.get(estado, 0) + 1
        if estado == "Z":
            zombies += 1
        total_threads += st["num_threads"]

        pct = _cpu_pct_pid(pid, st["utime"], st["stime"])
        status = procfs.leer_status(pid)
        rss = status.get("VmRSS", 0) if status else 0
        comm = st["comm"]
        top_cpu.append((pid, comm, pct))
        top_mem.append((pid, comm, rss))

    top_cpu.sort(key=lambda x: x[2], reverse=True)
    top_mem.sort(key=lambda x: x[2], reverse=True)

    vivos = set(pids)
    for muerto in [p for p in _prev_por_pid if p not in vivos]:
        del _prev_por_pid[muerto]

    return {
        "cpu_pct": cpu_pct,
        "loadavg": procfs.leer_loadavg(),
        "meminfo": procfs.leer_meminfo(),
        "btime": procfs.leer_btime(),
        "uptime": procfs.leer_uptime(),
        "total_procesos": len(pids),
        "por_estado": conteo_estados,
        "total_threads": total_threads,
        "zombies": zombies,
        "top_cpu": top_cpu[:3],
        "top_mem": top_mem[:3],
    }


def correr(cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val, cpu_quick_array):
    correr_analizador("sistema", cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val,
                       calcular, cpu_quick_array)
