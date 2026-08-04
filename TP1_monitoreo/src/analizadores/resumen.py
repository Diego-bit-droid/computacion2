"""
Analizador: Resumen (vista 1)
Clase 3: anatomía básica de un proceso. Clase 4: estados y wait() (zombies).
"""
import time
import procfs
from analizadores.base import correr_analizador

_prev = {}  # pid -> (utime+stime, timestamp) para el delta de CPU%


def _cpu_porcentaje(pid, utime, stime):
    ahora = time.time()
    total_ticks = utime + stime
    anterior = _prev.get(pid)
    _prev[pid] = (total_ticks, ahora)
    if anterior is None:
        return 0.0
    ticks_prev, t_prev = anterior
    dt = ahora - t_prev
    if dt <= 0:
        return 0.0
    delta_ticks = total_ticks - ticks_prev
    return round((delta_ticks / procfs.CLK_TCK) / dt * 100.0, 1)


def calcular(pids, verbose):
    resultado = {}
    for pid in pids:
        st = procfs.leer_stat(pid)
        if st is None:
            continue
        status = procfs.leer_status(pid)
        if status is None:
            continue
        uid = status.get("Uid", [0])[0]
        gid = status.get("Gid", [0])[0]
        resultado[pid] = {
            "pid": pid,
            "ppid": st["ppid"],
            "uid": uid,
            "usuario": procfs.uid_a_nombre(uid),
            "gid": gid,
            "grupo": procfs.gid_a_nombre(gid),
            "estado": st["state"],
            "comando": procfs.leer_cmdline(pid) or f"[{st['comm']}]",
            "comm": st["comm"],
            "cpu_pct": _cpu_porcentaje(pid, st["utime"], st["stime"]),
            "threads": status.get("Threads", 1),
            "rss_kb": status.get("VmRSS", 0),
        }
    # limpiar pids muertos del cache de CPU para no crecer sin límite
    vivos = set(pids)
    for muerto in [p for p in _prev if p not in vivos]:
        del _prev[muerto]
    return resultado


def correr(cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val):
    correr_analizador("resumen", cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val, calcular)
