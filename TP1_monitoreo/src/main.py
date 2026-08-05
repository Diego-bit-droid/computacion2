#!/usr/bin/env python3
"""
main.py — Monitor de Procesos y Threads (TP1 Computación II)

Orquesta la arquitectura multiproceso:
  Recolector (1 proceso) --Queue--> 7 Analizadores (1 proceso c/u) --Manager dict--> Display (proceso principal)

El Manager de multiprocessing.Manager() levanta, además, su propio proceso
servidor: ese proceso es, en la práctica, el "Agregador" que pide el
enunciado — es quien centraliza el acceso al snapshot compartido y serializa
las escrituras concurrentes de los 7 analizadores (podés verlo corriendo con
`ps -ef` dentro del contenedor mientras el monitor está andando).

Al Display le pasamos tres cosas más allá del snapshot:
  - cpu_quick: el Array('d', 4) de memoria compartida real, que lee en cada
    frame sin pagar una RPC al Manager (es la contraparte práctica de la
    comparación Manager vs Array que argumentamos en el README).
  - procesos: la lista de mp.Process hijos, para mostrar en pantalla el PID
    real de cada analizador y si sigue vivo (demo de `kill -9 <analizador>`).
  - estado_senal: dict común (mismo proceso que el hilo despachador) con la
    última señal atendida, para verla en la TUI sin abrir monitor.log.
"""
import curses
import json
import multiprocessing as mp
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import display
import recolector
import senales
from analizadores import resumen, memoria, fds, threads as vt_threads
from analizadores import senales as an_senales
from analizadores import scheduling, sistema
from analizadores.base import INTERVALOS_MINIMOS
VISTAS = ["resumen", "memoria", "fds", "threads", "senales", "scheduling", "sistema"]

MODULOS = {
    "resumen": resumen,
    "memoria": memoria,
    "fds": fds,
    "threads": vt_threads,
    "senales": an_senales,
    "scheduling": scheduling,
    "sistema": sistema,
}

CONFIG_PATH = os.environ.get("MONITOR_CONFIG", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"))


def cargar_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"intervalos": {}, "intervalo_recolector": 1.0, "orden_default": "cpu"}


def log_a_archivo(msg):
    linea = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        with open("monitor.log", "a") as f:
            f.write(linea + "\n")
    except OSError:
        pass


def hilo_senales(contexto):
    """
    Hilo auxiliar (no viola la arquitectura multiproceso: es sólo el
    despachador del self-pipe dentro del proceso principal, la lógica de
    negocio real -shutdown, reload, dump- corre acá pero coordina procesos
    aparte vía shutdown_evt / Values / Manager dict).
    """
    while not contexto["shutdown_evt"].is_set():
        if senales.hay_senales_pendientes(timeout=0.3):
            senales.procesar(contexto)


def main():
    cfg = cargar_config()
    defaults = cfg.get("intervalos", {})

    mp.set_start_method("fork", force=True)  # default en Linux; lo dejamos explícito
    manager = mp.Manager()
    snapshot = manager.dict()

    colas = {v: mp.Queue(maxsize=1) for v in VISTAS}
    intervalos = {v: mp.Value("d", max(INTERVALOS_MINIMOS.get(v, 0.5),
                                   float(defaults.get(v, 2.0)))) for v in VISTAS}
    verbose_val = mp.Value("b", 0)
    shutdown_evt = mp.Event()
    repintar_evt = mp.Event()
    cpu_quick = mp.Array("d", [0.0, 0.0, 0.0, 0.0])

    # Dict común (no primitiva de IPC): sólo lo tocan el hilo despachador de
    # señales y el Display, que corren en ESTE proceso.
    estado_senal = {"nombre": None, "detalle": "", "ts": 0.0}

    procesos = []
    procesos.append(mp.Process(
        target=recolector.correr,
        args=(colas, shutdown_evt, float(cfg.get("intervalo_recolector", 1.0))),
        name="recolector", daemon=True))

    for vista in VISTAS:
        modulo = MODULOS[vista]
        args = (colas[vista], snapshot, intervalos[vista], shutdown_evt, verbose_val)
        if vista == "sistema":
            args = args + (cpu_quick,)
        procesos.append(mp.Process(target=modulo.correr, args=args, name=f"analizador-{vista}", daemon=True))

    for p in procesos:
        p.start()
    log_a_archivo(f"Monitor iniciado. PID principal={os.getpid()}. Hijos: " +
                  ", ".join(f"{p.name}={p.pid}" for p in procesos))

    # Instalamos los handlers de señal recién ahora: así los hijos (ya forkeados)
    # no heredan el self-pipe ni el handler pensado para el proceso principal.
    senales.instalar_handlers()

    contexto = {
        "shutdown_evt": shutdown_evt,
        "intervalos": intervalos,
        "verbose_val": verbose_val,
        "snapshot": snapshot,
        "config_path": CONFIG_PATH,
        "repintar_evt": repintar_evt,
        "estado_senal": estado_senal,
        "log": log_a_archivo,
    }

    t_senales = threading.Thread(target=hilo_senales, args=(contexto,), daemon=True)
    t_senales.start()

    # Mapa PID -> rol, para que la lista no muestre 10 filas idénticas de
    # 'python3 src/main.py' (son todos forks del mismo intérprete).
    # manager._process es el proceso servidor que levanta multiprocessing.Manager:
    # es "privado" en la API, pero es exactamente el proceso que hace de Agregador
    # y queremos poder señalarlo en pantalla. Si en alguna versión de Python
    # cambiara, el except deja el resto funcionando igual.
    roles = {os.getpid(): "principal + display"}
    try:
        roles[manager._process.pid] = "manager = AGREGADOR"
    except AttributeError:
        log_a_archivo("No se pudo obtener el PID del proceso del Manager")
    for p in procesos:
        roles[p.pid] = p.name
    log_a_archivo("Roles: " + ", ".join(f"{k}={v}" for k, v in sorted(roles.items())))

    try:
        curses.wrapper(display.correr, snapshot, intervalos, verbose_val, shutdown_evt,
                       cfg, repintar_evt, cpu_quick, procesos, estado_senal, roles)
    except Exception as e:
        log_a_archivo(f"Display terminó con excepción: {e!r}")
    finally:
        shutdown_evt.set()
        log_a_archivo("Shutdown: terminando procesos hijos...")
        for p in procesos:
            p.join(timeout=3)
        for p in procesos:
            if p.is_alive():
                # OJO: acá NO sirve p.terminate(), que manda SIGTERM, porque
                # los hijos hacen signal.signal(SIGTERM, SIG_IGN) justamente
                # para que un Ctrl+C en la terminal no les llegue de forma
                # descoordinada (comparten grupo de proceso con el padre).
                # Como ya les pedimos el shutdown por shutdown_evt y no
                # respondieron en 3s, el único recurso que queda es SIGKILL,
                # que no se puede ignorar ni capturar.
                log_a_archivo(f"{p.name} no terminó a tiempo, forzando kill()")
                p.kill()
                p.join(timeout=1)
        manager.shutdown()
        log_a_archivo("Monitor detenido limpiamente.")


if __name__ == "__main__":
    main()
