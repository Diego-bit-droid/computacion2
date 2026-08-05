"""
Analizador: File Descriptors (vista 3)
Clase 5 (pipes / IPC básico, file descriptors).

Es la vista más cara de las 7: por cada proceso hay que listar
/proc/<pid>/fd y hacer un readlink por entrada. Por eso usamos
`leer_fds_con_total`, que recorre el directorio UNA sola vez y devuelve el
total junto con la lista ya recortada, en vez de un contar_fds() + un
leer_fds() que listaban el mismo directorio dos veces.
"""
import procfs
from analizadores.base import correr_analizador

LIMITE_NORMAL = 10
LIMITE_VERBOSE = 200


def calcular(pids, verbose):
    resultado = {}
    limite = LIMITE_VERBOSE if verbose else LIMITE_NORMAL
    for pid in pids:
        total, lista = procfs.leer_fds_con_total(pid, limite=limite)
        if total == 0:
            # Sin FDs: o es un zombie (ya los liberó al terminar) o no tenemos
            # permiso para mirar su /proc/<pid>/fd.
            continue
        resultado[pid] = {
            "total_fds": total,
            "fds": lista,
            "truncado": total > limite,
        }
    return resultado


def correr(cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val):
    correr_analizador("fds", cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val, calcular)
