"""
Analizador: Scheduling (vista 6)
Clase 9-10: scheduler, prioridades, políticas RT vs OTHER.
"""
import procfs
from analizadores.base import correr_analizador


def calcular(pids, verbose):
    resultado = {}
    for pid in pids:
        st = procfs.leer_stat(pid)
        status = procfs.leer_status(pid)
        if st is None or status is None:
            continue
        # El enunciado admite dos fuentes para la política: /proc/<pid>/sched o
        # el campo 41 de /proc/<pid>/stat. Preferimos sched (más explícito) y
        # caemos a stat si ese archivo no está disponible en este kernel.
        politica = procfs.leer_policy_sched(pid) or procfs.nombre_policy(st.get("policy"))
        resultado[pid] = {
            "nice": st["nice"],
            "priority": st["priority"],
            "policy": politica if politica else "?",
            "rt_priority": st["rt_priority"],
            "affinity": status.get("Cpus_allowed_list", "?"),
            "vol_ctx": status.get("voluntary_ctxt_switches", 0),
            "nonvol_ctx": status.get("nonvoluntary_ctxt_switches", 0),
            "utime": st["utime"],
            "stime": st["stime"],
            "sid": st["session"],
            "pgid": st["pgrp"],
        }
    return resultado


def correr(cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val):
    correr_analizador("scheduling", cola_pids, snapshot, intervalo_val, shutdown_evt, verbose_val, calcular)
