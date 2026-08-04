"""
Analizador: Memoria (vista 2)
Clase 3 (memoria virtual) y Clase 7 (mmap).
"""
import procfs
from analizadores.base import correr_analizador


def calcular(pids, verbose):
    resultado = {}
    for pid in pids:
        status = procfs.leer_status(pid)
        st = procfs.leer_stat(pid)
        if status is None or st is None:
            continue
        resultado[pid] = {
            "vm_size": status.get("VmSize", 0),
            "vm_rss": status.get("VmRSS", 0),
            "vm_hwm": status.get("VmHWM", 0),
            "vm_data": status.get("VmData", 0),
            "vm_stk": status.get("VmStk", 0),
            "vm_exe": status.get("VmExe", 0),
            "vm_lib": status.get("VmLib", 0),
            "vm_swap": status.get("VmSwap", 0),
            "minflt": st["minflt"],
            "majflt": st["majflt"],
            "segmentos": procfs.leer_maps_agrupado(pid),
        }
        if verbose:
            # modo verbose: además de los segmentos agrupados, mapeos individuales (más caro de leer)
            resultado[pid]["maps_detalle"] = procfs._leer_lineas(f"/proc/{pid}/maps")[:40]
    return resultado


def correr(cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val):
    correr_analizador("memoria", cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val, calcular)
