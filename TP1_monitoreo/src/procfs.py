"""
procfs.py
Funciones de bajo nivel para leer y parsear /proc.
Ningún componente del TP usa psutil ni nada que abstraiga /proc: todo pasa por acá.

Referencias: proc(5), Clase 3 (anatomía de procesos), Clase 4 (fork/exec/wait, zombies).
"""
import os
import re
import pwd
import grp

CLK_TCK = os.sysconf("SC_CLK_TCK")  # jiffies por segundo, normalmente 100

# --- helpers genéricos ------------------------------------------------------

def listar_pids():
    """Todos los PIDs actualmente vivos, leyendo los nombres de carpeta numéricos de /proc."""
    pids = []
    try:
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                pids.append(int(entry))
    except FileNotFoundError:
        pass
    return pids


def _leer(path):
    """
    Lectura defensiva de /proc: un proceso puede morir entre que lo listamos y
    que lo leemos (ESRCH), o podemos no tener permisos (EPERM/EACCES).
    Capturamos OSError entero y no las tres excepciones puntuales: /proc
    también puede devolver EINVAL o EIO en algunos archivos según el kernel, y
    en todos esos casos la respuesta correcta es la misma ("no hay dato para
    este proceso"), no romper el analizador.
    """
    try:
        with open(path, "r") as f:
            return f.read()
    except OSError:
        return None


def _leer_lineas(path):
    contenido = _leer(path)
    if contenido is None:
        return []
    return contenido.splitlines()


# --- /proc/<pid>/stat --------------------------------------------------------
# El campo 'comm' viene entre paréntesis y puede contener espacios/paréntesis,
# por eso no se puede hacer un split() ingenuo: buscamos el ')' final.

_STAT_CAMPOS = [
    "pid", "comm", "state", "ppid", "pgrp", "session", "tty_nr", "tpgid",
    "flags", "minflt", "cminflt", "majflt", "cmajflt", "utime", "stime",
    "cutime", "cstime", "priority", "nice", "num_threads", "itrealvalue",
    "starttime", "vsize", "rss", "rsslim", "startcode", "endcode", "startstack",
    "kstkesp", "kstkeip", "signal", "blocked", "sigignore", "sigcatch",
    "wchan", "nswap", "cnswap", "exit_signal", "processor", "rt_priority",
    "policy", "delayacct_blkio_ticks", "guest_time", "cguest_time",
    "start_data", "end_data", "start_brk", "arg_start", "arg_end",
    "env_start", "env_end", "exit_code",
]


def leer_stat(pid, tid=None):
    """Parsea /proc/<pid>/stat (o /proc/<pid>/task/<tid>/stat). Campos indexados desde 1 según proc(5)."""
    if tid is None:
        path = f"/proc/{pid}/stat"
    else:
        path = f"/proc/{pid}/task/{tid}/stat"
    contenido = _leer(path)
    if contenido is None:
        return None
    try:
        ini = contenido.index("(")
        fin = contenido.rindex(")")
    except ValueError:
        return None
    comm = contenido[ini + 1:fin]
    resto = contenido[fin + 2:].split()
    valores = [str(pid), comm, ] + resto
    d = {}
    for nombre, val in zip(_STAT_CAMPOS, valores):
        if nombre in ("comm", "state"):
            d[nombre] = val
        else:
            try:
                d[nombre] = int(val)
            except ValueError:
                d[nombre] = val
    return d


# --- /proc/<pid>/status -------------------------------------------------------

_STATUS_INT_KEYS = {
    "Pid", "PPid", "Threads", "VmPeak", "VmSize", "VmLck", "VmPin", "VmHWM",
    "VmRSS", "VmData", "VmStk", "VmExe", "VmLib", "VmPTE", "VmSwap",
    "voluntary_ctxt_switches", "nonvoluntary_ctxt_switches",
}


def leer_status(pid, tid=None):
    """Parsea /proc/<pid>/status en un dict. Los campos Vm* vienen en kB. None si el proceso no existe."""
    path = f"/proc/{pid}/status" if tid is None else f"/proc/{pid}/task/{tid}/status"
    contenido = _leer(path)
    if contenido is None:
        return None
    d = {}
    for linea in contenido.splitlines():
        if ":" not in linea:
            continue
        clave, valor = linea.split(":", 1)
        clave, valor = clave.strip(), valor.strip()
        if clave in ("Uid", "Gid"):
            partes = valor.split()
            d[clave] = [int(p) for p in partes]
        elif clave == "Cpus_allowed_list":
            d[clave] = valor
        elif clave in _STATUS_INT_KEYS:
            m = re.match(r"(-?\d+)", valor)
            d[clave] = int(m.group(1)) if m else 0
        elif clave in ("SigBlk", "SigIgn", "SigCgt", "SigPnd", "ShdPnd"):
            try:
                d[clave] = int(valor, 16)
            except ValueError:
                d[clave] = 0
        else:
            d[clave] = valor
    return d


# --- /proc/<pid>/cmdline -------------------------------------------------------

def leer_cmdline(pid):
    contenido = _leer(f"/proc/{pid}/cmdline")
    if not contenido:
        return ""
    return " ".join(p for p in contenido.split("\x00") if p)


# --- usuario / grupo -----------------------------------------------------------

_cache_usuarios = {}
_cache_grupos = {}


def uid_a_nombre(uid):
    if uid not in _cache_usuarios:
        try:
            _cache_usuarios[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _cache_usuarios[uid] = str(uid)
    return _cache_usuarios[uid]


def gid_a_nombre(gid):
    if gid not in _cache_grupos:
        try:
            _cache_grupos[gid] = grp.getgrgid(gid).gr_name
        except KeyError:
            _cache_grupos[gid] = str(gid)
    return _cache_grupos[gid]


# --- /proc/<pid>/maps -----------------------------------------------------------

def _es_libreria(path):
    """
    ¿Este mapeo corresponde a una librería compartida?
    Contemplamos el sufijo " (deleted)" que agrega el kernel cuando el archivo
    fue reemplazado en disco mientras seguía mapeado (típico tras un upgrade
    con procesos viejos todavía vivos): sin esto no matchean con endswith(".so").
    """
    limpio = path.replace(" (deleted)", "")
    return limpio.endswith(".so") or ".so." in limpio


def leer_maps_agrupado(pid):
    """
    Agrupa las líneas de /proc/<pid>/maps por tipo de segmento:
    text (r-xp del binario), data/heap, stack, shared (memoria compartida/librerías con 's' o file-backed .so).
    Devuelve tamaño total en kB por categoría.
    """
    grupos = {"text": 0, "data_heap": 0, "stack": 0, "shared_libs": 0, "otros": 0}
    for linea in _leer_lineas(f"/proc/{pid}/maps"):
        partes = linea.split(None, 5)
        if len(partes) < 5:
            continue
        rango, perms = partes[0], partes[1]
        path = partes[5] if len(partes) > 5 else ""
        try:
            ini_s, fin_s = rango.split("-")
            tam_kb = (int(fin_s, 16) - int(ini_s, 16)) // 1024
        except ValueError:
            continue

        # OJO con el orden de las ramas: el segmento r-xp de una librería
        # compartida (libc, por ejemplo) cumple TANTO "es ejecutable" como
        # "es un .so". Si preguntáramos primero por los permisos, todo el
        # código de las librerías caería en "text" y ese grupo dejaría de
        # significar "el binario del proceso", que es lo que queremos mostrar.
        if "[stack" in path:
            grupos["stack"] += tam_kb
        elif "[heap]" in path:
            grupos["data_heap"] += tam_kb
        elif _es_libreria(path):
            grupos["shared_libs"] += tam_kb
        elif "x" in perms and path and not path.startswith("["):
            grupos["text"] += tam_kb
        elif "w" in perms and (path == "" or path.startswith("[")):
            grupos["data_heap"] += tam_kb
        else:
            grupos["otros"] += tam_kb
    return grupos


# --- /proc/<pid>/fd --------------------------------------------------------------

def _inferir_tipo_fd(destino):
    if destino.startswith("socket:"):
        return "socket"
    if destino.startswith("pipe:"):
        return "pipe"
    if destino.startswith("/dev/pts") or destino.startswith("/dev/tty"):
        return "tty"
    if destino.startswith("anon_inode:"):
        return "anon_inode"
    if destino.startswith("/memfd:"):
        return "memfd"
    if destino.startswith("/"):
        return "file"
    return "otro"


def leer_fds_con_total(pid, limite=None):
    """
    Devuelve (total_fds, lista_de_fds) recorriendo /proc/<pid>/fd UNA sola vez.

    Listar ese directorio y hacer readlink de cada entrada es la operación más
    cara de las 7 vistas (un proceso puede tener cientos de FDs abiertos), así
    que no queremos pagar dos listdir del mismo directorio para saber el total
    y para leer los primeros N. El total sale del len() de la lista completa;
    `limite` sólo recorta cuántos readlink hacemos, que es la parte cara.
    """
    base = f"/proc/{pid}/fd"
    try:
        nombres = os.listdir(base)
    except OSError:
        return 0, []
    total = len(nombres)
    nombres.sort(key=lambda x: int(x) if x.isdigit() else 0)
    if limite is not None:
        nombres = nombres[:limite]
    resultado = []
    for n in nombres:
        try:
            destino = os.readlink(f"{base}/{n}")
        except OSError:
            destino = "?"  # el fd se cerró entre el listdir y el readlink
        resultado.append({"fd": n, "destino": destino, "tipo": _inferir_tipo_fd(destino)})
    return total, resultado


def leer_fds(pid, limite=None):
    """Lista descriptores de archivo abiertos: número, destino (readlink) y tipo inferido."""
    return leer_fds_con_total(pid, limite=limite)[1]


def contar_fds(pid):
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return 0


# --- /proc/<pid>/task (threads / LWPs) --------------------------------------------

def listar_tids(pid):
    try:
        return [int(t) for t in os.listdir(f"/proc/{pid}/task")]
    except OSError:
        return []


def leer_comm_thread(pid, tid):
    c = _leer(f"/proc/{pid}/task/{tid}/comm")
    return c.strip() if c else "?"


# --- señales: máscaras hex -> nombres --------------------------------------------

# Orden estándar de Linux (bit 1 = señal 1). man 7 signal.
_NOMBRES_SENAL = {
    1: "SIGHUP", 2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 5: "SIGTRAP",
    6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE", 9: "SIGKILL", 10: "SIGUSR1",
    11: "SIGSEGV", 12: "SIGUSR2", 13: "SIGPIPE", 14: "SIGALRM", 15: "SIGTERM",
    16: "SIGSTKFLT", 17: "SIGCHLD", 18: "SIGCONT", 19: "SIGSTOP", 20: "SIGTSTP",
    21: "SIGTTIN", 22: "SIGTTOU", 23: "SIGURG", 24: "SIGXCPU", 25: "SIGXFSZ",
    26: "SIGVTALRM", 27: "SIGPROF", 28: "SIGWINCH", 29: "SIGIO", 30: "SIGPWR",
    31: "SIGSYS",
}


def _nombre_senal(num):
    """
    Nombre legible de una señal. Las 1..31 son las clásicas de POSIX; de la 32
    en adelante son de tiempo real, pero en Linux con NPTL glibc se reserva la
    32 y la 33 para el runtime de threads, así que SIGRTMIN "visible" arranca
    en la 34. Por eso no alcanza con f"RT_{num}": hay que numerarlas relativas
    a 34 para que coincidan con lo que muestran `kill -l` y htop.
    """
    if num in _NOMBRES_SENAL:
        return _NOMBRES_SENAL[num]
    if num in (32, 33):
        return f"SIG{num}(glibc)"
    if num >= 34:
        return "SIGRTMIN" if num == 34 else f"SIGRTMIN+{num - 34}"
    return f"SIG{num}"


def decodificar_mascara_senales(mascara_hex):
    """Convierte una máscara de 64 bits (int) en la lista de nombres de señales activas (bit i = señal i+1)."""
    nombres = []
    for bit in range(1, 65):
        if mascara_hex & (1 << (bit - 1)):
            nombres.append(_nombre_senal(bit))
    return nombres


# --- scheduling policy -------------------------------------------------------------

_POLICIAS = {0: "OTHER", 1: "FIFO", 2: "RR", 3: "BATCH", 5: "IDLE", 6: "DEADLINE"}


def nombre_policy(numero):
    """Traduce el número de política de scheduling (campo 41 de /proc/<pid>/stat) a su nombre."""
    if numero is None:
        return None
    return _POLICIAS.get(numero, f"?{numero}")


def leer_policy_sched(pid):
    """Lee la política de scheduling desde /proc/<pid>/sched (línea 'policy'), si está disponible."""
    contenido = _leer(f"/proc/{pid}/sched")
    if contenido:
        m = re.search(r"policy\s*:\s*(\d+)", contenido)
        if m:
            return _POLICIAS.get(int(m.group(1)), f"?{m.group(1)}")
    return None


# --- /proc/stat global --------------------------------------------------------------

def leer_cpu_global():
    """Primera línea 'cpu ...' de /proc/stat: user nice system idle iowait irq softirq steal."""
    for linea in _leer_lineas("/proc/stat"):
        if linea.startswith("cpu "):
            campos = [int(x) for x in linea.split()[1:]]
            campos += [0] * (8 - len(campos))
            claves = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"]
            return dict(zip(claves, campos))
    return None


def leer_btime():
    for linea in _leer_lineas("/proc/stat"):
        if linea.startswith("btime"):
            return int(linea.split()[1])
    return None


def leer_loadavg():
    contenido = _leer("/proc/loadavg")
    if not contenido:
        return None
    partes = contenido.split()
    return {"load1": float(partes[0]), "load5": float(partes[1]), "load15": float(partes[2])}


def leer_uptime():
    contenido = _leer("/proc/uptime")
    if not contenido:
        return None
    partes = contenido.split()
    return {"uptime": float(partes[0]), "idle_total": float(partes[1])}


def leer_meminfo():
    d = {}
    for linea in _leer_lineas("/proc/meminfo"):
        if ":" not in linea:
            continue
        clave, resto = linea.split(":", 1)
        m = re.match(r"\s*(\d+)", resto)
        d[clave.strip()] = int(m.group(1)) if m else 0
    return d
