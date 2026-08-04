"""
base.py
Loop genérico que reutilizan los 7 analizadores. Cada analizador es un
multiprocessing.Process independiente (Clase 8-9) que:
  1. espera su turno según su propio intervalo (multiprocessing.Value, ajustable en caliente),
  2. toma la última lista de PIDs que dejó el Recolector en su Queue,
  3. calcula su porción de datos,
  4. escribe el resultado completo en snapshot[clave] = {"data":..., "ts": time.time()}

Escribir el sub-dict *completo* de una sola asignación evita que otro proceso
lea un estado a medio construir (no hace falta Lock explícito: la asignación
a una clave del dict del Manager es una sola llamada RPC atómica al proceso
servidor del Manager).
"""
import queue
import signal
import time

# Intervalo mínimo de refresco por vista, según la tabla del enunciado.
# Vive acá (y no en display.py) porque es una propiedad del analizador: es el
# ritmo por debajo del cual leer /proc para esa dimensión sale demasiado caro.
# El Display lo importa para no dejar bajar el Value por debajo de este piso,
# y el loop de abajo lo vuelve a aplicar como defensa: aunque alguien escriba
# un valor menor en el Value (por ejemplo vía SIGHUP con un config.json
# inconsistente), el analizador nunca corre más rápido que su mínimo.
INTERVALOS_MINIMOS = {
    "resumen": 0.5,
    "memoria": 1.0,
    "fds": 2.0,
    "threads": 0.5,
    "senales": 5.0,
    "scheduling": 5.0,
    "sistema": 1.0,
}


def obtener_ultimos_pids(cola, cache):
    """Vacía la cola quedándose con el mensaje más reciente (evita que se acumulen si el analizador es lento)."""
    ultimo = None
    while True:
        try:
            ultimo = cola.get_nowait()
        except queue.Empty:
            break
    if ultimo is not None:
        cache["pids"] = ultimo
    return cache.get("pids", [])


def correr_analizador(nombre_clave, cola_pids, snapshot, intervalo_val, shutdown_evt,
                       verbose_val, funcion_calculo, *extra):
    """
    Loop principal reutilizado por los 7 analizadores.
    funcion_calculo(pids, verbose:bool, *extra) -> dict con los datos ya armados.
    """
    # Los analizadores no manejan señales por su cuenta: todo shutdown se
    # coordina a través de shutdown_evt, controlado por el proceso principal.
    # Si no ignoráramos SIGINT acá, Ctrl+C en una terminal interactiva le
    # llegaría también a estos procesos hijos (mismo grupo de proceso) y cada
    # uno tiraría su propio KeyboardInterrupt de forma descoordinada.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    cache = {"pids": []}
    while not shutdown_evt.is_set():
        inicio = time.time()
        pids = obtener_ultimos_pids(cola_pids, cache)
        verbose = bool(verbose_val.value)
        try:
            datos = funcion_calculo(pids, verbose, *extra)
            snapshot[nombre_clave] = {"data": datos, "ts": time.time()}
        except Exception as e:  # un analizador no debe tumbar el monitor entero
            snapshot[nombre_clave] = {"data": snapshot.get(nombre_clave, {}).get("data", {}),
                                       "ts": time.time(), "error": str(e)}

        intervalo = max(INTERVALOS_MINIMOS.get(nombre_clave, 0.5), float(intervalo_val.value))
        transcurrido = time.time() - inicio
        espera = max(0.0, intervalo - transcurrido)
        shutdown_evt.wait(espera)
