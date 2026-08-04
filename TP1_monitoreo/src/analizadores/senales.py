"""
Analizador: Señales (vista 5)
Clase 6: señales, máscaras de 64 bits, handlers.
"""
import procfs
from analizadores.base import correr_analizador


def calcular(pids, verbose):
    resultado = {}
    for pid in pids:
        status = procfs.leer_status(pid)
        if status is None:
            continue
        resultado[pid] = {
            "bloqueadas": procfs.decodificar_mascara_senales(status.get("SigBlk", 0)),
            "ignoradas": procfs.decodificar_mascara_senales(status.get("SigIgn", 0)),
            "con_handler": procfs.decodificar_mascara_senales(status.get("SigCgt", 0)),
            "pendientes_proceso": procfs.decodificar_mascara_senales(status.get("SigPnd", 0)),
            "pendientes_grupo": procfs.decodificar_mascara_senales(status.get("ShdPnd", 0)),
        }
    return resultado


def correr(cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val):
    correr_analizador("senales", cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val, calcular)
