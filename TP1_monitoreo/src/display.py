"""
display.py
Interfaz de texto (curses, sin dependencias externas -> no necesita red para
`pip install` dentro del contenedor, lo que hace más robusto el
`docker compose up --build`).

Usa curses.nodelay + timeout en vez de un thread aparte para el teclado: la
propia llamada a getch() no bloquea, así que no hace falta un hilo extra
(el enunciado lo deja como opcional). Todo el estado de la UI (vista activa,
cursor, filtros, pin, orden) vive sólo en este proceso: no necesita
sincronización porque nadie más lo toca.

Layout (de arriba hacia abajo, siempre visible sin importar la vista):
  fila 0  : barra de vistas
  fila 1  : header global del sistema  -> CPU se lee del Array compartido
            `cpu_quick` (memoria compartida real, sin RPC al Manager);
            MEM/LOAD/PROC del snapshot del Manager.
  fila 2  : estado de la UI + última señal recibida por el monitor
  fila 3  : barra de analizadores: nombre[PID]intervalo de cada proceso hijo,
            en verde si está vivo y en rojo si murió (probalo con kill -9).
  fila 5+ : lista de procesos
  abajo   : panel de detalle de la vista activa + línea de estado
"""
import curses
import time

VISTAS = [
    ("resumen", "Resumen", ["1", "r"]),
    ("memoria", "Memoria", ["2", "m"]),
    ("fds", "FDs", ["3", "f"]),
    ("threads", "Threads", ["4", "t"]),
    ("senales", "Señales", ["5", "s"]),
    ("scheduling", "Scheduling", ["6", "p"]),
    ("sistema", "Sistema", ["7", "g"]),
]
TECLA_A_VISTA = {}
for _clave, _titulo, _teclas in VISTAS:
    for _t in _teclas:
        TECLA_A_VISTA[_t] = _clave

ORDEN_CICLO = ["cpu", "rss", "pid"]

# Piso por vista al bajar el intervalo con '-': la vista FDs (listdir+readlink
# de todos los FDs de todos los procesos) es la más cara, así que no dejamos
# que baje de 2s ni queriendo.
# Reparto vertical: la lista se lleva hasta MAX_FILAS_LISTA filas de datos y el
# panel de detalle se queda con el resto (siempre >= MIN_LINEAS_DETALLE).
MAX_FILAS_LISTA = 15
MIN_FILAS_LISTA = 3
MIN_LINEAS_DETALLE = 3

MIN_INTERVALO = {
    "resumen": 0.5, "memoria": 1.0, "fds": 2.0, "threads": 0.5,
    "senales": 1.0, "scheduling": 1.0, "sistema": 0.5,
}

# --- colores ---------------------------------------------------------------
# Los pares se inicializan una sola vez en correr(); si la terminal no soporta
# color (TERM=dumb, por ejemplo), _color() devuelve 0 y todo sigue funcionando
# con los atributos monocromos de siempre.
_HAY_COLOR = False
C_TITULO, C_OK, C_ERROR, C_AVISO, C_DATO = 1, 2, 3, 4, 5


def _init_colores():
    global _HAY_COLOR
    if not curses.has_colors():
        _HAY_COLOR = False
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        fondo = -1
    except curses.error:
        fondo = curses.COLOR_BLACK
    curses.init_pair(C_TITULO, curses.COLOR_CYAN, fondo)
    curses.init_pair(C_OK, curses.COLOR_GREEN, fondo)
    curses.init_pair(C_ERROR, curses.COLOR_RED, fondo)
    curses.init_pair(C_AVISO, curses.COLOR_YELLOW, fondo)
    curses.init_pair(C_DATO, curses.COLOR_WHITE, fondo)
    _HAY_COLOR = True


def _color(par):
    return curses.color_pair(par) if _HAY_COLOR else 0


def _fmt_kb(kb):
    if kb is None:
        return "-"
    if kb >= 1024 * 1024:
        return f"{kb/1024/1024:.1f}G"
    if kb >= 1024:
        return f"{kb/1024:.1f}M"
    return f"{kb}K"


def _dibujar_tokens(stdscr, y, ancho, tokens):
    """Dibuja una fila compuesta por (texto, atributo), respetando el ancho."""
    x = 0
    for texto, attr in tokens:
        if x >= ancho:
            break
        try:
            stdscr.addnstr(y, x, texto[:ancho - x], ancho - x, attr)
        except curses.error:
            pass
        x += len(texto)


class EstadoUI:
    def __init__(self, config):
        self.vista = "resumen"
        self.cursor = 0
        self.pin_pid = None
        self.filtro_cmd = config.get("filtro_cmd_default", "") or ""
        self.filtro_user = config.get("filtro_user_default", "") or ""
        self.orden = config.get("orden_default", "cpu")
        self.modo_input = None  # None | "filtro_cmd" | "filtro_user"
        self.buffer_input = ""
        self.mensaje = ""
        self.mensaje_ts = 0
        self.mostrar_ayuda = False
        self.pids_visibles = []  # cache del último frame, para resolver Enter (pin)
        self.intervalo_recolector = float(config.get("intervalo_recolector", 1.0))

    def set_mensaje(self, txt):
        self.mensaje = txt
        self.mensaje_ts = time.time()


def _procesos_filtrados_ordenados(resumen_data, ui):
    items = list(resumen_data.values())
    if ui.filtro_cmd:
        items = [p for p in items if ui.filtro_cmd.lower() in p["comando"].lower()
                  or ui.filtro_cmd.lower() in p["comm"].lower()]
    if ui.filtro_user:
        items = [p for p in items if ui.filtro_user.lower() in p["usuario"].lower()]

    if ui.orden == "cpu":
        items.sort(key=lambda p: p["cpu_pct"], reverse=True)
    elif ui.orden == "rss":
        items.sort(key=lambda p: p["rss_kb"], reverse=True)
    else:
        items.sort(key=lambda p: p["pid"])
    return items


def _pid_seleccionado(items, ui):
    if ui.pin_pid is not None:
        for p in items:
            if p["pid"] == ui.pin_pid:
                return ui.pin_pid
        ui.pin_pid = None  # el proceso pineado ya murió
    if not items:
        return None
    ui.cursor = max(0, min(ui.cursor, len(items) - 1))
    return items[ui.cursor]["pid"]


# --- header global ----------------------------------------------------------

def _linea_cpu(cpu_quick, sis):
    """
    CPU sale del Array('d', 4) compartido: es una lectura directa de memoria,
    sin pickle ni RPC contra el proceso servidor del Manager. Por eso podemos
    permitirnos leerla en CADA frame (hasta 5 veces por segundo) mientras que
    el resto del snapshot se refresca al ritmo del analizador Sistema.
    """
    user, system, idle, iowait = cpu_quick[0], cpu_quick[1], cpu_quick[2], cpu_quick[3]
    uso = max(0.0, 100.0 - idle)
    mem = sis.get("meminfo", {}) or {}
    total = mem.get("MemTotal", 0)
    disp = mem.get("MemAvailable")
    if disp is None:
        disp = mem.get("MemFree", 0) + mem.get("Cached", 0) + mem.get("Buffers", 0)
    usada = max(0, total - disp)
    load = sis.get("loadavg", {}) or {}
    return (
        f"CPU {uso:5.1f}% (user {user:.1f} sys {system:.1f} idle {idle:.1f} iowait {iowait:.1f})"
        f" | MEM {_fmt_kb(usada)}/{_fmt_kb(total)}"
        f" | LOAD {load.get('load1','-')} {load.get('load5','-')} {load.get('load15','-')}"
        f" | PROC {sis.get('total_procesos',0)} THR {sis.get('total_threads',0)}"
        f" Z:{sis.get('zombies',0)}"
    )


def _tokens_analizadores(procesos, intervalos, ui):
    """
    Barra que muestra los procesos hijos reales: nombre[PID]intervalo.
    is_alive() hace un waitpid no bloqueante, así que si alguien mata un
    analizador con `kill -9` el cambio se ve acá en el próximo frame (y esa
    vista queda congelada, que es exactamente lo que documentamos).
    """
    tokens = [("Analizadores: ", curses.A_DIM)]
    for p in procesos:
        vivo = p.is_alive()
        if p.name == "recolector":
            etiqueta = f"recolector[{p.pid}]{ui.intervalo_recolector:.1f}s "
        else:
            clave = p.name.replace("analizador-", "")
            val = intervalos.get(clave)
            seg = f"{val.value:.1f}s" if val is not None else "?"
            etiqueta = f"{clave}[{p.pid}]{seg} "
        if not vivo:
            etiqueta = etiqueta.rstrip() + " MUERTO "
        attr = (_color(C_OK) if vivo else _color(C_ERROR) | curses.A_BOLD)
        tokens.append((etiqueta, attr))
    return tokens


def _texto_ultima_senal(estado_senal):
    """
    estado_senal es un dict común de Python, no una primitiva de IPC: el hilo
    despachador de señales y el Display corren en el MISMO proceso, así que
    alcanza con la atomicidad que ya garantiza el GIL para asignar una clave.
    """
    if not estado_senal or not estado_senal.get("nombre"):
        return "últ. señal: -"
    hace = int(time.time() - estado_senal.get("ts", 0))
    detalle = estado_senal.get("detalle", "")
    return f"últ. señal: {estado_senal['nombre']}" + (f" ({detalle})" if detalle else "") + f" hace {hace}s"


# --- lista de procesos ------------------------------------------------------

_ENCABEZADO = (f"{'S':1} {'PID':>7} {'PPID':>7} {'USUARIO':<10} {'GID':>5} "
               f"{'E':1} {'CPU%':>6} {'RSS':>8} {'THR':>4}  COMANDO")


def _etiqueta_rol(pid, roles):
    """
    Los 10 procesos del monitor comparten el mismo comando ('python3 src/main.py')
    porque son forks del mismo intérprete: sin etiqueta son 10 filas idénticas.
    `roles` mapea PID -> rol dentro de nuestra arquitectura (principal/display,
    recolector, analizador-<vista>, manager/agregador).
    """
    rol = roles.get(pid) if roles else None
    return f"   <- {rol}" if rol else ""


def _attr_estado(estado):
    if estado == "Z":
        return _color(C_ERROR) | curses.A_BOLD
    if estado == "R":
        return _color(C_OK)
    if estado in ("D", "T"):
        return _color(C_AVISO)
    return curses.A_NORMAL


def _dibujar_lista(stdscr, y0, alto, ancho, items, ui, pid_sel, roles=None):
    filas_visibles = max(1, alto - 1)
    inicio = max(0, ui.cursor - filas_visibles + 1) if ui.cursor >= filas_visibles else 0
    fin = min(len(items), inicio + filas_visibles)
    encabezado = _ENCABEZADO
    if len(items) > filas_visibles:
        encabezado = f"{_ENCABEZADO}   [{inicio+1}-{fin} de {len(items)}]"
    stdscr.addnstr(y0, 0, encabezado[:ancho], ancho,
                   curses.A_REVERSE | curses.A_BOLD | _color(C_TITULO))
    for i, p in enumerate(items[inicio:inicio + filas_visibles]):
        idx_real = inicio + i
        marca = ">" if idx_real == ui.cursor else ("*" if p["pid"] == ui.pin_pid else " ")
        fila = (f"{marca:1} {p['pid']:>7} {p['ppid']:>7} {p['usuario'][:10]:<10} "
                f"{p['gid']:>5} {p['estado']:1} {p['cpu_pct']:>6.1f} "
                f"{_fmt_kb(p['rss_kb']):>8} {p['threads']:>4}  {p['comando']}"
                f"{_etiqueta_rol(p['pid'], roles)}")
        atributo = _attr_estado(p["estado"])
        if roles and p["pid"] in roles:
            atributo |= _color(C_TITULO)
        if p["pid"] == pid_sel:
            atributo |= curses.A_BOLD
            if idx_real == ui.cursor:
                atributo |= curses.A_REVERSE
        if p["pid"] == ui.pin_pid:
            atributo |= curses.A_UNDERLINE
        try:
            stdscr.addnstr(y0 + 1 + i, 0, fila[:ancho], ancho, atributo)
        except curses.error:
            pass


# --- paneles de detalle (uno por vista) -------------------------------------

def _detalle_resumen(snap, pid, ancho):
    d = snap.get("resumen", {}).get("data", {}).get(pid)
    if not d:
        return ["(sin datos de resumen para este proceso todavía)"]
    return [
        f"PID {d['pid']}  PPID {d['ppid']}  UID {d['uid']} ({d['usuario']})  GID {d['gid']} ({d['grupo']})",
        f"Estado: {d['estado']}   Threads: {d['threads']}   CPU%: {d['cpu_pct']}   RSS: {_fmt_kb(d['rss_kb'])}",
        f"Comando: {d['comando']}",
    ]


def _detalle_memoria(snap, pid, ancho):
    d = snap.get("memoria", {}).get("data", {}).get(pid)
    if not d:
        return ["(sin datos de memoria)"]
    seg = d.get("segmentos") or {}
    return [
        f"VmSize {_fmt_kb(d['vm_size'])}  VmRSS {_fmt_kb(d['vm_rss'])}  VmHWM {_fmt_kb(d['vm_hwm'])}  VmSwap {_fmt_kb(d['vm_swap'])}",
        f"VmData {_fmt_kb(d['vm_data'])}  VmStk {_fmt_kb(d['vm_stk'])}  VmExe {_fmt_kb(d['vm_exe'])}  VmLib {_fmt_kb(d['vm_lib'])}",
        f"Page faults -> minor: {d['minflt']}  major: {d['majflt']}",
        f"Segmentos -> text {seg.get('text',0)}K  data/heap {seg.get('data_heap',0)}K  stack {seg.get('stack',0)}K  shared/libs {seg.get('shared_libs',0)}K  otros {seg.get('otros',0)}K",
    ]


def _detalle_fds(snap, pid, ancho):
    d = snap.get("fds", {}).get("data", {}).get(pid)
    if not d:
        return ["(sin datos de FDs -- proceso zombie o sin permisos)"]
    lineas = [f"Total FDs abiertos: {d['total_fds']}" + ("  (lista truncada, activá verbose con SIGUSR2)" if d.get("truncado") else "")]
    for fd in d["fds"]:
        lineas.append(f"  fd {fd['fd']:>4}  [{fd['tipo']:<10}]  {fd['destino']}")
    return lineas


def _detalle_threads(snap, pid, ancho):
    d = snap.get("threads", {}).get("data", {}).get(pid)
    if not d:
        return ["(sin datos de threads)"]
    lineas = [f"Cantidad de threads (LWPs): {d['cantidad']}"]
    lineas.append(f"{'TID':>7} {'NOMBRE':<16} {'S':1} {'CPU%':>6} {'VOL':>6} {'NONVOL':>7}")
    for t in d["threads"]:
        lineas.append(f"{t['tid']:>7} {t['nombre'][:16]:<16} {t['estado']:1} {t['cpu_pct']:>6.1f} {t['vol_ctx']:>6} {t['nonvol_ctx']:>7}")
    return lineas


def _detalle_senales(snap, pid, ancho):
    d = snap.get("senales", {}).get("data", {}).get(pid)
    if not d:
        return ["(sin datos de señales)"]
    def fmt(lst):
        return ", ".join(lst) if lst else "(ninguna)"
    return [
        f"Bloqueadas (SigBlk):         {fmt(d['bloqueadas'])}",
        f"Ignoradas (SigIgn):          {fmt(d['ignoradas'])}",
        f"Con handler propio (SigCgt): {fmt(d['con_handler'])}",
        f"Pendientes proceso (SigPnd): {fmt(d['pendientes_proceso'])}",
        f"Pendientes grupo (ShdPnd):   {fmt(d['pendientes_grupo'])}",
    ]


def _detalle_scheduling(snap, pid, ancho):
    d = snap.get("scheduling", {}).get("data", {}).get(pid)
    if not d:
        return ["(sin datos de scheduling)"]
    return [
        f"Nice: {d['nice']}   Priority: {d['priority']}   Policy: {d['policy']}   RT priority: {d['rt_priority']}",
        f"CPU affinity: {d['affinity']}",
        f"Context switches -> voluntarios: {d['vol_ctx']}   involuntarios: {d['nonvol_ctx']}",
        f"utime: {d['utime']} ticks   stime: {d['stime']} ticks   SID: {d['sid']}   PGID: {d['pgid']}",
    ]


def _detalle_sistema(snap, pid, ancho):
    d = snap.get("sistema", {}).get("data", {})
    if not d:
        return ["(sin datos de sistema)"]
    cpu = d.get("cpu_pct", {})
    mem = d.get("meminfo", {})
    load = d.get("loadavg", {}) or {}
    uptime = d.get("uptime", {}) or {}
    lineas = [
        f"CPU  user {cpu.get('user',0):>5.1f}%  system {cpu.get('system',0):>5.1f}%  idle {cpu.get('idle',0):>5.1f}%  iowait {cpu.get('iowait',0):>5.1f}%",
        f"Load average: {load.get('load1','-')}  {load.get('load5','-')}  {load.get('load15','-')}   Uptime: {uptime.get('uptime',0):.0f}s",
        f"Memoria total {_fmt_kb(mem.get('MemTotal',0))}  libre {_fmt_kb(mem.get('MemFree',0))}  buffers {_fmt_kb(mem.get('Buffers',0))}  cache {_fmt_kb(mem.get('Cached',0))}  swap {_fmt_kb(mem.get('SwapTotal',0)-mem.get('SwapFree',0))}",
        f"Procesos: {d.get('total_procesos',0)}   Threads totales: {d.get('total_threads',0)}   Zombies: {d.get('zombies',0)}",
        f"Por estado: {d.get('por_estado',{})}",
        "Top 3 CPU: " + ", ".join(f"{c[1]}({c[0]}) {c[2]}%" for c in d.get("top_cpu", [])),
        "Top 3 MEM: " + ", ".join(f"{m[1]}({m[0]}) {_fmt_kb(m[2])}" for m in d.get("top_mem", [])),
    ]
    return lineas


_DETALLE_POR_VISTA = {
    "resumen": _detalle_resumen,
    "memoria": _detalle_memoria,
    "fds": _detalle_fds,
    "threads": _detalle_threads,
    "senales": _detalle_senales,
    "scheduling": _detalle_scheduling,
    "sistema": _detalle_sistema,
}

AYUDA = [
    "1-7 o r/m/f/t/s/p/g : cambiar de vista",
    "flechas arriba/abajo : navegar lista de procesos",
    "Enter : pin/unpin del proceso seleccionado",
    "/ : filtrar por comando   u : filtrar por usuario",
    "c : ciclar orden (CPU% / RSS / PID)",
    "+ / - : ajustar intervalo de la vista activa",
    "q : salir     h o ? : esta ayuda",
    "",
    "La barra 'Analizadores' muestra el PID real de cada proceso hijo y su",
    "intervalo. Si matás uno (kill -9 <pid>) se pone en rojo como MUERTO y",
    "esa vista deja de refrescarse, pero el resto del monitor sigue andando.",
    "",
    "Señales del proceso monitor (enviar con kill -SIGX <pid>):",
    "SIGINT/SIGTERM: shutdown limpio   SIGHUP: recargar config.json",
    "SIGUSR1: dump snapshot a JSON     SIGUSR2: toggle modo verbose",
    "",
    "Presioná cualquier tecla para volver...",
]


def correr(stdscr, snapshot, intervalos, verbose_val, shutdown_evt, config, repintar_evt,
           cpu_quick, procesos, estado_senal, roles=None):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(200)
    _init_colores()
    ui = EstadoUI(config)

    while not shutdown_evt.is_set():
        try:
            _frame(stdscr, snapshot, intervalos, verbose_val, ui,
                   cpu_quick, procesos, estado_senal, roles)
        except curses.error:
            pass  # terminal muy chica en este instante; se recupera en el próximo frame

        if repintar_evt.is_set():
            stdscr.clear()
            repintar_evt.clear()

        try:
            tecla = stdscr.getkey()
        except curses.error:
            tecla = None

        if tecla:
            _manejar_tecla(tecla, ui, intervalos, snapshot, shutdown_evt)


def _manejar_tecla(tecla, ui, intervalos, snapshot, shutdown_evt):
    if ui.modo_input:
        if tecla in ("\n", "\r"):
            if ui.modo_input == "filtro_cmd":
                ui.filtro_cmd = ui.buffer_input
            else:
                ui.filtro_user = ui.buffer_input
            ui.set_mensaje(f"Filtro aplicado: '{ui.buffer_input}'")
            ui.modo_input = None
            ui.buffer_input = ""
        elif tecla in ("KEY_BACKSPACE", "\b", "\x7f"):
            ui.buffer_input = ui.buffer_input[:-1]
        elif tecla == "\x1b":  # ESC cancela
            ui.modo_input = None
            ui.buffer_input = ""
        elif len(tecla) == 1 and tecla.isprintable():
            ui.buffer_input += tecla
        return

    if ui.mostrar_ayuda:
        ui.mostrar_ayuda = False
        return

    if tecla in TECLA_A_VISTA:
        ui.vista = TECLA_A_VISTA[tecla]
        ui.cursor = 0
    elif tecla == "KEY_UP":
        ui.cursor = max(0, ui.cursor - 1)
    elif tecla == "KEY_DOWN":
        ui.cursor += 1
    elif tecla in ("\n", "\r", "KEY_ENTER"):
        if 0 <= ui.cursor < len(ui.pids_visibles):
            pid_bajo_cursor = ui.pids_visibles[ui.cursor]
            if ui.pin_pid == pid_bajo_cursor:
                ui.pin_pid = None
                ui.set_mensaje("Pin quitado")
            else:
                ui.pin_pid = pid_bajo_cursor
                ui.set_mensaje(f"Proceso {pid_bajo_cursor} pineado")
    elif tecla == "/":
        ui.modo_input = "filtro_cmd"
        ui.buffer_input = ""
    elif tecla == "u":
        ui.modo_input = "filtro_user"
        ui.buffer_input = ""
    elif tecla == "c":
        idx = ORDEN_CICLO.index(ui.orden)
        ui.orden = ORDEN_CICLO[(idx + 1) % len(ORDEN_CICLO)]
        ui.set_mensaje(f"Orden: {ui.orden}")
    elif tecla in ("+", "="):
        val = intervalos[ui.vista]
        with val.get_lock():
            val.value = round(val.value + 0.5, 2)
        ui.set_mensaje(f"Intervalo de '{ui.vista}': {val.value:.1f}s")
    elif tecla == "-":
        val = intervalos[ui.vista]
        piso = MIN_INTERVALO.get(ui.vista, 0.5)
        with val.get_lock():
            nuevo = max(piso, round(val.value - 0.5, 2))
            tocaba_piso = (val.value <= piso)
            val.value = nuevo
        if tocaba_piso:
            ui.set_mensaje(f"'{ui.vista}' ya está en su intervalo mínimo ({piso:.1f}s)")
        else:
            ui.set_mensaje(f"Intervalo de '{ui.vista}': {val.value:.1f}s")
    elif tecla == "q":
        shutdown_evt.set()
    elif tecla in ("h", "?"):
        ui.mostrar_ayuda = True


def _frame(stdscr, snapshot, intervalos, verbose_val, ui, cpu_quick, procesos, estado_senal, roles=None):
    stdscr.erase()
    alto, ancho = stdscr.getmaxyx()

    # Dos lecturas al Manager por frame (resumen + sistema). El CPU% NO sale de
    # acá: sale del Array compartido, justamente para no pagar una tercera RPC.
    resumen = snapshot.get("resumen", {}).get("data", {})
    sis = snapshot.get("sistema", {}).get("data", {}) or {}

    items = _procesos_filtrados_ordenados(resumen, ui)
    pid_sel = _pid_seleccionado(items, ui)
    ui.pids_visibles = [p["pid"] for p in items]

    # fila 0: barra de vistas
    tokens_vistas = []
    for i, (clave, titulo, _) in enumerate(VISTAS):
        if clave == ui.vista:
            tokens_vistas.append((f"*{i+1}:{titulo.upper()}*  ",
                                  curses.A_BOLD | curses.A_REVERSE | _color(C_TITULO)))
        else:
            tokens_vistas.append((f"[{i+1}:{titulo}]  ", _color(C_TITULO)))
    _dibujar_tokens(stdscr, 0, ancho, tokens_vistas)

    # fila 1: header global (siempre visible, no sólo en la vista Sistema)
    stdscr.addnstr(1, 0, _linea_cpu(cpu_quick, sis)[:ancho], ancho,
                   curses.A_BOLD | _color(C_DATO))

    # fila 2: estado de la UI + última señal recibida por el monitor
    filtros = []
    if ui.filtro_cmd:
        filtros.append(f"cmd~{ui.filtro_cmd}")
    if ui.filtro_user:
        filtros.append(f"user~{ui.filtro_user}")
    info = (f"orden={ui.orden} {' '.join(filtros)}  verbose={'ON' if verbose_val.value else 'OFF'}"
            f"  intervalo={intervalos[ui.vista].value:.1f}s"
            f"  pin={ui.pin_pid if ui.pin_pid is not None else '-'}"
            f"  |  {_texto_ultima_senal(estado_senal)}")
    stdscr.addnstr(2, 0, info[:ancho], ancho, curses.A_DIM)

    # fila 3: procesos hijos reales (PID + intervalo + vivo/muerto)
    _dibujar_tokens(stdscr, 3, ancho, _tokens_analizadores(procesos, intervalos, ui))

    # --- reparto del alto disponible ---------------------------------------
    # Le damos PRIORIDAD a la lista (hasta MAX_FILAS_LISTA) en vez de partir la
    # pantalla al medio: con la mitad fija, en una terminal baja la lista se
    # quedaba con el encabezado y cero filas. El detalle se queda con lo que
    # sobra, nunca menos de MIN_LINEAS_DETALLE.
    y_lista = 5
    fn_detalle = _DETALLE_POR_VISTA[ui.vista]
    lineas_detalle = fn_detalle(snapshot, pid_sel, ancho)

    disponible = max(4, alto - y_lista - 2)  # -2: línea separadora y pie de página
    alto_lista = max(MIN_FILAS_LISTA, min(len(items), MAX_FILAS_LISTA)) + 1  # +1 = encabezado
    alto_lista = max(2, min(alto_lista, disponible - MIN_LINEAS_DETALLE))
    # si el detalle es corto y sobra lugar, la lista se estira más allá del tope
    sobra = disponible - alto_lista - len(lineas_detalle)
    if sobra > 0:
        alto_lista += min(sobra, max(0, len(items) + 1 - alto_lista))

    _dibujar_lista(stdscr, y_lista, alto_lista, ancho, items, ui, pid_sel, roles)

    y_detalle = y_lista + alto_lista + 1
    ts_vista = snapshot.get(ui.vista, {}).get("ts")
    edad = f"  (dato de hace {time.time() - ts_vista:.1f}s)" if ts_vista else "  (sin datos aún)"
    titulo_panel = f"--- Detalle: {ui.vista.upper()} -- PID {pid_sel if pid_sel else '-'}{edad} "
    stdscr.addnstr(y_detalle - 1, 0,
                   (titulo_panel + "-" * ancho)[:min(ancho, 120)], ancho,
                   curses.A_DIM | _color(C_TITULO))

    for i, linea in enumerate(lineas_detalle):
        if y_detalle + i >= alto - 1:
            break
        try:
            stdscr.addnstr(y_detalle + i, 0, linea[:ancho], ancho)
        except curses.error:
            pass

    if ui.modo_input:
        prompt = f"{'Filtrar comando' if ui.modo_input=='filtro_cmd' else 'Filtrar usuario'}: {ui.buffer_input}_"
        stdscr.addnstr(alto - 1, 0, prompt[:ancho], ancho, curses.A_REVERSE)
    elif time.time() - ui.mensaje_ts < 3 and ui.mensaje:
        stdscr.addnstr(alto - 1, 0, ui.mensaje[:ancho], ancho, curses.A_REVERSE | _color(C_AVISO))
    else:
        pie = "1-7 vista | ↑↓ nav | Enter pin | / cmd | u user | c orden | +/- intervalo | q salir | h ayuda"
        stdscr.addnstr(alto - 1, 0, pie[:ancho], ancho, curses.A_DIM)

    if ui.mostrar_ayuda:
        _dibujar_ayuda(stdscr, alto, ancho)

    stdscr.refresh()


def _dibujar_ayuda(stdscr, alto, ancho):
    h = min(len(AYUDA) + 2, alto - 2)
    w = min(max(len(l) for l in AYUDA) + 4, ancho - 2)
    y0 = max(0, (alto - h) // 2)
    x0 = max(0, (ancho - w) // 2)
    win = curses.newwin(h, w, y0, x0)
    win.box()
    for i, linea in enumerate(AYUDA):
        if i + 1 >= h - 1:
            break
        try:
            win.addnstr(i + 1, 2, linea, w - 4)
        except curses.error:
            pass
    win.refresh()
