"""
senales.py
Manejo de las señales que recibe el monitor (no confundir con
analizadores/senales.py, que es la VISTA que muestra las señales de otros
procesos leyendo /proc/<pid>/status).

Usamos el patrón self-pipe (Clase 6): el handler real de signal.signal()
sólo puede llamar funciones async-signal-safe. Escribir un byte a un fd con
os.write() lo es; hacer print(), tocar un dict de Python, etc. no lo es
(podría interrumpir al intérprete en medio de una operación no atómica).
Por eso el handler se limita a escribir el número de señal al pipe, y todo
el trabajo real (terminar hijos, recargar config, dumpear el snapshot) se
hace en el loop principal, fuera del contexto de señal.

Además, cada señal atendida se registra en contexto["estado_senal"] para que
el Display la muestre en pantalla: es la forma de demostrar en vivo que
SIGHUP/SIGUSR1/SIGUSR2 llegaron y se procesaron, sin tener que ir a mirar
monitor.log desde otra terminal. Ojo: ese registro se hace acá, en el hilo
despachador (contexto seguro), NO dentro del handler.
"""
import json
import os
import select
import signal
import time

_pipe_r, _pipe_w = None, None


def instalar_handlers():
    """Crea el self-pipe y registra los handlers. Debe llamarse en el proceso principal."""
    global _pipe_r, _pipe_w
    _pipe_r, _pipe_w = os.pipe()
    os.set_blocking(_pipe_r, False)

    def _handler(signum, frame):
        try:
            os.write(_pipe_w, bytes([signum % 256]))
        except OSError:
            pass  # pipe lleno: la señal se puede perder, pero no rompemos el handler

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP,
              signal.SIGUSR1, signal.SIGUSR2, signal.SIGWINCH):
        signal.signal(s, _handler)

    return _pipe_r


def fd_lectura():
    return _pipe_r


def hay_senales_pendientes(timeout=0.0):
    """select() no bloqueante sobre el extremo de lectura del self-pipe."""
    if _pipe_r is None:
        return False
    listos, _, _ = select.select([_pipe_r], [], [], timeout)
    return bool(listos)


def leer_senales_pendientes():
    """Devuelve la lista de números de señal recibidos desde la última lectura."""
    if _pipe_r is None:
        return []
    try:
        datos = os.read(_pipe_r, 4096)
    except OSError:
        return []
    return list(datos)


def _registrar(contexto, signum, detalle):
    """
    Deja constancia de la última señal atendida para que la TUI la muestre.
    estado_senal es un dict común: el hilo despachador y el Display viven en
    el mismo proceso, así que no hace falta ninguna primitiva de IPC (a
    diferencia de todo lo que se comparte con los procesos analizadores).
    """
    est = contexto.get("estado_senal")
    if est is None:
        return
    try:
        nombre = signal.Signals(signum).name
    except ValueError:
        nombre = str(signum)
    est["nombre"] = nombre
    est["detalle"] = detalle
    est["ts"] = time.time()


def procesar(contexto):
    """
    Ejecuta las acciones correspondientes a las señales pendientes.
    contexto: dict con las referencias que necesitan las acciones:
        shutdown_evt, intervalos (dict de Value), verbose_val (Value),
        snapshot (Manager dict), config_path, repintar_evt, estado_senal, log.
    """
    for signum in leer_senales_pendientes():
        if signum in (signal.SIGINT % 256, signal.SIGTERM % 256):
            _registrar(contexto, signum, "shutdown")
            contexto["log"](f"Señal {signal.Signals(signum).name if signum in (2, 15) else signum} recibida: shutdown limpio")
            contexto["shutdown_evt"].set()
        elif signum == signal.SIGHUP % 256:
            _recargar_config(contexto)
        elif signum == signal.SIGUSR1 % 256:
            _dump_snapshot(contexto)
        elif signum == signal.SIGUSR2 % 256:
            with contexto["verbose_val"].get_lock():
                contexto["verbose_val"].value = 0 if contexto["verbose_val"].value else 1
            estado = "ON" if contexto["verbose_val"].value else "OFF"
            _registrar(contexto, signum, f"verbose {estado}")
            contexto["log"](f"Modo verbose: {estado}")
        elif signum == signal.SIGWINCH % 256:
            _registrar(contexto, signum, "repaint")
            contexto["repintar_evt"].set()


def _recargar_config(contexto):
    """SIGHUP: relee config.json y aplica intervalos por vista."""
    try:
        with open(contexto["config_path"]) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _registrar(contexto, signal.SIGHUP, "reload FALLÓ")
        contexto["log"](f"SIGHUP: no se pudo recargar config.json ({e})")
        return
    intervalos = cfg.get("intervalos", {})
    for vista, val in contexto["intervalos"].items():
        if vista in intervalos:
            with val.get_lock():
                val.value = float(intervalos[vista])
    _registrar(contexto, signal.SIGHUP, "config recargada")
    contexto["log"]("SIGHUP: configuración recargada desde config.json")


def _dump_snapshot(contexto):
    """SIGUSR1: vuelca el snapshot actual a dump_<timestamp>.json"""
    ts = int(time.time())
    nombre = f"dump_{ts}.json"
    try:
        plano = {k: v for k, v in contexto["snapshot"].items()}
        with open(nombre, "w") as f:
            json.dump(plano, f, indent=2, default=str, ensure_ascii=False)
        _registrar(contexto, signal.SIGUSR1, f"dump -> {nombre}")
        contexto["log"](f"SIGUSR1: snapshot volcado en {nombre}")
    except Exception as e:
        _registrar(contexto, signal.SIGUSR1, "dump FALLÓ")
        contexto["log"](f"SIGUSR1: error al volcar snapshot ({e})")
