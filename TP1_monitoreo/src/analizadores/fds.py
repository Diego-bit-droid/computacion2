"""
Analizador: File Descriptors (vista 3)
Clase 5 (pipes / IPC básico, file descriptors).
"""
import procfs
from analizadores.base import correr_analizador

LIMITE_NORMAL = 10
LIMITE_VERBOSE = 200


def calcular(pids, verbose):
    resultado = {}
    limite = LIMITE_VERBOSE if verbose else LIMITE_NORMAL
    for pid in pids:
        total = procfs.contar_fds(pid)
        if total == 0:
            continue
        resultado[pid] = {
            "total_fds": total,
            "fds": procfs.leer_fds(pid, limite=limite),
            "truncado": total > limite,
        }
    return resultado


def correr(cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val):
    correr_analizador("fds", cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val, calcular)
