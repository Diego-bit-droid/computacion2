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
"""
import curses
import time

from analizadores.base import INTERVALOS_MINIMOS

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


def _fmt_kb(kb):
    if kb is None:
        return "-"
    if kb >= 1024 * 1024:
        return f"{kb/1024/1024:.1f}G"
    if kb >= 1024:
        return f"{kb/1024:.1f}M"
    return f"{kb}K"


def _fmt_btime(btime):
    """btime de /proc/stat viene en segundos desde epoch: lo mostramos legible."""
    if not btime:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(btime))


class EstadoUI:
    def __init__(self, config):
        self.vista = "resumen"
        self.cursor = 0
        self.pin_pid = None
        self.filtro_cmd = ""
        self.filtro_user = ""
        self.orden = "cpu"
        self.aplicar_defaults(config)
        self.modo_input = None  # None | "filtro_cmd" | "filtro_user"
        self.buffer_input = ""
        self.mensaje = ""
        self.mensaje_ts = 0
        self.mostrar_ayuda = False
        self.pids_visibles = []  # cache del último frame, para resolver Enter (pin)

    def aplicar_defaults(self, config):
        """
        Vuelca los valores por defecto de config.json al estado de la UI.
        Se llama al arrancar y otra vez cada vez que SIGHUP recarga el archivo,
        que es lo que pide el enunciado ("recarga intervalos por vista Y
        filtros default").
        """
        self.orden = config.get("orden_default", "cpu")
        if self.orden not in ORDEN_CICLO:
            self.orden = "cpu"
        self.filtro_cmd = config.get("filtro_cmd_default", "") or ""
        self.filtro_user = config.get("filtro_user_default", "") or ""

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


def _dibujar_lista(stdscr, y0, alto, ancho, items, ui, pid_sel):
    encabezado = f"{'PID':>7} {'USR':<8} {'S':1} {'CPU%':>6} {'RSS':>8} {'THR':>4}  COMANDO"
    stdscr.addnstr(y0, 0, encabezado[:ancho], ancho, curses.A_REVERSE)
    filas_visibles = alto - 1
    inicio = max(0, ui.cursor - filas_visibles + 1) if ui.cursor >= filas_visibles else 0
    for i, p in enumerate(items[inicio:inicio + filas_visibles]):
        idx_real = inicio + i
        fila = f"{p['pid']:>7} {p['usuario'][:8]:<8} {p['estado']:1} {p['cpu_pct']:>6.1f} {_fmt_kb(p['rss_kb']):>8} {p['threads']:>4}  {p['comando']}"
        atributo = curses.A_NORMAL
        if p["pid"] == pid_sel:
            atributo = curses.A_BOLD | curses.A_REVERSE if idx_real == ui.cursor else curses.A_BOLD
        if p["pid"] == ui.pin_pid:
            atributo |= curses.A_UNDERLINE
        try:
            stdscr.addnstr(y0 + 1 + i, 0, fila[:ancho], ancho, atributo)
        except curses.error:
            pass


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
        return ["(sin datos de FDs -- puede requerir permisos)"]
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
        f"Bloqueadas (SigBlk):        {fmt(d['bloqueadas'])}",
        f"Ignoradas (SigIgn):         {fmt(d['ignoradas'])}",
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
        f"Boot time: {_fmt_btime(d.get('btime'))}",
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
    "Señales del proceso monitor (enviar con kill -SIGX <pid>):",
    "SIGINT/SIGTERM: shutdown limpio   SIGHUP: recargar config.json",
    "SIGUSR1: dump snapshot a JSON     SIGUSR2: toggle modo verbose",
    "",
    "Presioná cualquier tecla para volver...",
]


def correr(stdscr, snapshot, intervalos, verbose_val, shutdown_evt, config, repintar_evt,
           cpu_quick, recargar_ui_evt):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(200)
    ui = EstadoUI(config)

    while not shutdown_evt.is_set():
        # SIGHUP releyó config.json: volvemos a aplicar orden y filtros default.
        # `config` es el mismo objeto dict que actualiza senales._recargar_config
        # (ambos corren en el proceso principal), así que ya trae los valores nuevos.
        if recargar_ui_evt.is_set():
            ui.aplicar_defaults(config)
            ui.set_mensaje("SIGHUP: config.json recargado")
            recargar_ui_evt.clear()

        try:
            _frame(stdscr, snapshot, intervalos, verbose_val, ui, cpu_quick)
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
        minimo = INTERVALOS_MINIMOS.get(ui.vista, 0.5)
        with val.get_lock():
            nuevo = max(minimo, round(val.value - 0.5, 2))
            topeado = nuevo == val.value
            val.value = nuevo
        if topeado:
            ui.set_mensaje(f"'{ui.vista}' ya está en su mínimo ({minimo:.1f}s)")
        else:
            ui.set_mensaje(f"Intervalo de '{ui.vista}': {val.value:.1f}s")
    elif tecla == "q":
        shutdown_evt.set()
    elif tecla in ("h", "?"):
        ui.mostrar_ayuda = True


def _frame(stdscr, snapshot, intervalos, verbose_val, ui, cpu_quick):
    stdscr.erase()
    alto, ancho = stdscr.getmaxyx()

    # Lectura directa del Array('d', 4) de memoria compartida que escribe el
    # analizador Sistema: son 4 doubles, sin pickle ni RPC contra el proceso
    # servidor del Manager. Por eso lo podemos leer en CADA frame (hasta 5
    # veces por segundo) sin que se note, a diferencia del snapshot, que sólo
    # tocamos para la lista y el panel de detalle.
    cpu_u, cpu_s, cpu_i, cpu_w = cpu_quick[0], cpu_quick[1], cpu_quick[2], cpu_quick[3]

    resumen = snapshot.get("resumen", {}).get("data", {})
    items = _procesos_filtrados_ordenados(resumen, ui)
    pid_sel = _pid_seleccionado(items, ui)
    ui.pids_visibles = [p["pid"] for p in items]

    titulo_vistas = "  ".join(
        f"[{i+1}:{titulo}]" if clave != ui.vista else f"*{i+1}:{titulo.upper()}*"
        for i, (clave, titulo, _) in enumerate(VISTAS)
    )
    stdscr.addnstr(0, 0, titulo_vistas[:ancho], ancho, curses.A_BOLD)

    filtros = []
    if ui.filtro_cmd:
        filtros.append(f"cmd~{ui.filtro_cmd}")
    if ui.filtro_user:
        filtros.append(f"user~{ui.filtro_user}")
    info = (f"CPU u{cpu_u:.1f}% s{cpu_s:.1f}% i{cpu_i:.1f}% w{cpu_w:.1f}%  |  "
            f"orden={ui.orden} {' '.join(filtros)}  verbose={'ON' if verbose_val.value else 'OFF'}  "
            f"intervalo={intervalos[ui.vista].value:.1f}s (min {INTERVALOS_MINIMOS.get(ui.vista, 0.5):.1f}s)")
    stdscr.addnstr(1, 0, info[:ancho], ancho, curses.A_DIM)

    alto_lista = max(3, (alto - 6) // 2)
    _dibujar_lista(stdscr, 3, alto_lista, ancho, items, ui, pid_sel)

    y_detalle = 3 + alto_lista + 1
    stdscr.addnstr(y_detalle - 1, 0, "-" * min(ancho, 100), ancho, curses.A_DIM)
    fn_detalle = _DETALLE_POR_VISTA[ui.vista]
    for i, linea in enumerate(fn_detalle(snapshot, pid_sel, ancho)):
        if y_detalle + i >= alto - 2:
            break
        try:
            stdscr.addnstr(y_detalle + i, 0, linea[:ancho], ancho)
        except curses.error:
            pass

    # OJO: en la última fila hay que dejar libre la última columna. Escribir en
    # la esquina inferior derecha hace que curses intente scrollear y devuelva
    # ERR, lo que abortaba el frame ANTES del refresh() de abajo: en una
    # terminal de 80 columnas la pantalla no se repintaba nunca.
    ancho_pie = max(0, ancho - 1)
    if ui.modo_input:
        prompt = f"{'Filtrar comando' if ui.modo_input=='filtro_cmd' else 'Filtrar usuario'}: {ui.buffer_input}_"
        stdscr.addnstr(alto - 1, 0, prompt[:ancho_pie], ancho_pie, curses.A_REVERSE)
    elif time.time() - ui.mensaje_ts < 3 and ui.mensaje:
        stdscr.addnstr(alto - 1, 0, ui.mensaje[:ancho_pie], ancho_pie, curses.A_REVERSE)
    else:
        pie = "1-7 vista | ↑↓ nav | Enter pin | / cmd | u user | c orden | +/- intervalo | q salir | h ayuda"
        stdscr.addnstr(alto - 1, 0, pie[:ancho_pie], ancho_pie, curses.A_DIM)

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
