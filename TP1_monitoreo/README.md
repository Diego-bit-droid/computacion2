# TP1 — Monitor de Procesos y Threads

Monitor de sistema en tiempo real tipo `htop`, con foco en mostrar la
anatomía interna de cada proceso (memoria, FDs, threads, señales,
scheduling) leyendo `/proc` directamente. Arquitectura 100% multiproceso:
un Recolector, 7 Analizadores en paralelo, un Agregador (el propio proceso
servidor de `multiprocessing.Manager`) y una TUI en `curses`.

## Descripción general

El monitor lista los procesos vivos del sistema (o del contenedor, según
cómo se lo levante) y, para cada uno, expone 7 dimensiones distintas de
información — resumen, memoria, file descriptors, threads, señales,
scheduling y estadísticas globales del sistema — cada una calculada por un
proceso independiente que lee `/proc` a su propio ritmo.

La interfaz siempre muestra arriba una lista de procesos (PID, usuario,
estado, CPU%, RSS, threads, comando) y abajo un panel de detalle que cambia
según la vista activa. No se usa `psutil` ni ninguna librería que abstraiga
el acceso al kernel: todo el parseo de `/proc` está en `src/procfs.py`.

### Cómo correr y testear

```bash
# Todo junto, como pide el enunciado:
docker compose up --build

# Durante el desarrollo, sin Docker (necesita Linux, por /proc):
make dev
# o directamente:
MONITOR_CONFIG=$(pwd)/config.json python3 src/main.py

# Tests unitarios de procfs.py (no requieren pytest, usan unittest de la stdlib;
# corren igual con pytest si lo tenés instalado):
python3 -m unittest discover -s tests -v
# o: make test (si hay pytest disponible)
```

Una vez adentro, con el teclado:

| Tecla | Acción |
|---|---|
| `1..7` / `r m f t s p g` | Cambiar de vista (Resumen/Memoria/FDs/Threads/Señales/Scheduling/Sistema) |
| `↑ ↓` | Navegar la lista de procesos |
| `Enter` | Pin/unpin del proceso seleccionado |
| `/` | Filtrar por nombre de comando |
| `u` | Filtrar por usuario |
| `c` | Ciclar orden: CPU% → RSS → PID |
| `+` / `-` | Ajustar el intervalo de refresco de la vista activa |
| `q` | Salir (shutdown limpio) |
| `h` / `?` | Ayuda |

Y desde otra terminal, `docker exec` o `kill` para probar señales:

```bash
docker exec tp1-monitor sh -c 'kill -USR1 1'   # dump del snapshot a data/dump_<ts>.json
docker exec tp1-monitor sh -c 'kill -USR2 1'   # toggle modo verbose
docker exec tp1-monitor sh -c 'kill -HUP  1'   # recargar config.json
```

(El PID `1` es el del propio `main.py` dentro del contenedor, porque no
hay ningún proceso init intermedio en el `entrypoint`.)

## Diagrama de arquitectura

```
                    ┌────────────────────────────────────────────┐
   /proc  ────────► │              RECOLECTOR (proceso)           │
   (todo el         │  cada 1s: os.listdir("/proc") -> lista PIDs │
    sistema)         └───────┬──────┬──────┬──────┬──────┬────┬───┘
                              │      │      │      │      │    │  (7 Queues, maxsize=1,
                              ▼      ▼      ▼      ▼      ▼    ▼   "última lista gana")
                         ┌─────┐┌─────┐┌────┐┌───────┐┌─────┐┌──────────┐┌────────┐
                         │Resum││Memor││FDs ││Threads││Señal││Scheduling││Sistema │
                         │ 2s  ││ 3s  ││ 5s ││  2s   ││ 10s ││   10s    ││  2s    │
                         └──┬──┘└──┬──┘└─┬──┘└───┬───┘└──┬──┘└────┬─────┘└───┬────┘
                            │      │     │       │       │        │          │  cada uno lee
                            │      │     │       │       │        │          │  /proc/<pid>/...
                            │      │     │       │       │        │          │  por su cuenta
                            ▼      ▼     ▼       ▼       ▼        ▼          ▼
                       ┌──────────────────────────────────────────────────────────┐
                       │        AGREGADOR = proceso servidor del Manager           │
                       │        snapshot = manager.dict()                          │
                       │  { "resumen":{data,ts}, "memoria":{data,ts}, ... }        │
                       │  (cada analizador escribe snapshot[clave]=... completo,   │
                       │   asignación atómica vía RPC al proceso del Manager)      │
                       └───────────────────────────┬──────────────────────────────┘
                                                     │ lee (RPC)
                                                     ▼
                       ┌──────────────────────────────────────────────────────────┐
                       │      DISPLAY (curses, en el proceso principal)            │
                       │  lista de procesos + panel de detalle según vista activa  │
                       └──────────────────────────────────────────────────────────┘

  Además, fuera del flujo de datos:

  señal Unix -> handler async-signal-safe (self-pipe) -> hilo "hilo_senales"
              en el proceso principal -> shutdown_evt / Value(intervalo) /
              Value(verbose) / dump JSON / reload config.json

  intervalos:  7 x multiprocessing.Value('d', ...) — el Display los escribe
               con +/- y cada Analizador los relee en cada vuelta de su loop.
  cpu_quick:   multiprocessing.Array('d', 4) — [user%, system%, idle%, iowait%]
               escrito por el analizador Sistema, leído directo por Display.
  shutdown:    multiprocessing.Event() — lo setea el proceso principal al
               recibir SIGINT/SIGTERM/q, todos los procesos hijos lo pollean.
```

## Decisiones de diseño argumentadas

### ¿Por qué `Queue` para Recolector → Analizadores?

Es el mecanismo natural para "productor único, muchos consumidores
independientes": el Recolector no necesita saber nada de lo que hace cada
analizador con la lista de PIDs, sólo publicarla. Usamos `Queue(maxsize=1)`
y en `recolector.py` vaciamos la cola antes de poner el valor nuevo
(`_publicar`): no nos interesa que un analizador lento acumule un backlog
de "listas de PIDs" viejas, sólo que la próxima vez que mire la cola
encuentre la más reciente posible. Si usáramos una `Pipe` en cambio,
tendríamos que armar 7 pipes punto a punto de todas formas (una `Pipe` es
1 a 1), así que `Queue` fue más directo para "1 productor, N consumidores".

### ¿Por qué `Manager.dict` y no `Value`/`Array` para el snapshot?

El snapshot completo es una estructura arbitraria y variable: dicts
anidados con listas de threads, listas de FDs, dicts de segmentos de
memoria, etc. `Value` y `Array` están pensados para tipos C fijos
(`'d'`, `'i'`, `'b'`, arrays de tamaño fijo) — no sirven para "un dict de
PID a un dict con 10 campos, algunos de ellos listas". `Manager.dict()`
sí soporta objetos Python arbitrarios porque el proceso servidor del
Manager los serializa (pickle) en cada operación.

El precio es que cada `snapshot[clave] = ...` es una llamada RPC al
proceso del Manager, más cara que escribir memoria realmente compartida.
Por eso el `cpu_quick` (4 números que cambian todo el tiempo y que quiere
leer el Display en cada frame, hasta 5 veces por segundo) sí lo sacamos a
un `Array('d', 4)` de memoria compartida real: ahí no hace falta pickle ni
RPC, es una lectura directa de memoria. Es la comparación concreta que
pide el enunciado: Manager para datos ricos y poco frecuentes, Array/Value
para escalares simples y muy frecuentes.

### ¿Cómo se manejaron las race conditions?

La regla que seguimos en los 7 analizadores es: **armar la estructura de
datos completa en una variable local y recién al final hacer una única
asignación `snapshot[clave] = estructura_completa`** (ver
`analizadores/base.py::correr_analizador`). Nunca hacemos
`snapshot[clave]["algo"] = x` (mutar un sub-dict in place), porque eso
requeriría dos operaciones RPC (leer el dict, modificarlo, escribirlo de
vuelta) con una ventana entre medio donde otro proceso podría pisar el
cambio. Al reemplazar la clave entera de una sola vez, cualquier lector
(el Display) siempre ve o bien el snapshot viejo completo o el nuevo
completo, nunca un estado a medio escribir — sin necesitar un `Lock`
explícito, porque el propio Manager serializa las llamadas a su proceso
servidor.

El otro punto de race condition posible es el ajuste de intervalos: el
Display escribe `intervalos[vista].value` desde el proceso principal
mientras el analizador correspondiente lo está leyendo en su loop. Ahí sí
usamos explícitamente `with val.get_lock():` al escribir (cada `Value`
trae su propio `Lock` interno), aunque en la práctica una escritura de un
`double` de 8 bytes ya es atómica a nivel de hardware en x86-64; lo dejamos
igual para no depender de esa garantía de la plataforma.

### ¿Por qué esos intervalos por defecto?

Ordenamos las vistas por "cuán caro es leer todo lo que muestran" más
"cuán rápido cambia":

- **Resumen, Threads, Sistema (2s)**: son los datos que más cambian
  segundo a segundo (CPU%, estados) y son relativamente baratos de leer
  (`stat`/`status`, sin recorrer directorios grandes).
- **Memoria (3s)**: agrupar `/proc/<pid>/maps` recorriendo todas las
  líneas es más caro que leer `status`, y la memoria de un proceso no
  suele cambiar de forma dramática entre 2 y 3 segundos.
- **FDs (5s)**: `os.listdir` + `os.readlink` por cada FD de cada proceso
  es, con diferencia, la operación más cara de las 7 (puede haber
  cientos de FDs por proceso); además el conjunto de FDs abiertos suele
  ser bastante estable.
- **Señales y Scheduling (10s)**: son datos de configuración del proceso
  que casi nunca cambian salvo que alguien reprograme handlers o cambie
  la política de scheduling explícitamente, así que no vale la pena
  refrescarlos seguido.

Los intervalos mínimos (que limitan cuánto se puede bajar con `-`) siguen
la misma lógica: nunca dejamos que la vista más cara (FDs) baje de 2s,
para que un usuario ansioso no pueda, sin querer, saturar el sistema
pidiendo `listdir`+`readlink` de todos los FDs de todos los procesos
varias veces por segundo.

## Conceptos del curso aplicados

- **Clase 3 (anatomía de procesos, `/proc`)**: `procfs.py` entero, en
  particular el parseo de `/proc/<pid>/stat` (con el truco del `comm`
  entre paréntesis, que puede contener espacios) y de `/proc/<pid>/maps`
  para agrupar segmentos de memoria por tipo (text/heap/stack/shared).
- **Clase 4 (fork/exec/wait, zombies, COW)**: en `analizadores/sistema.py`,
  contamos zombies mirando el campo `State` de `/proc/<pid>/stat` (`Z`).
  Un zombie es exactamente lo que vimos en clase: un proceso que ya
  terminó pero cuyo padre todavía no llamó a `wait()`/`waitpid()`, por
  eso el kernel mantiene su entry en la tabla de procesos con ese estado.
- **Clase 5 (pipes, IPC básico, FDs)**: `analizadores/fds.py` lista los FDs
  de cada proceso vía `/proc/<pid>/fd` e infiere el tipo (`pipe:[N]`,
  `socket:[N]`, tty, archivo regular) a partir del destino del symlink,
  que es justamente la forma en que vimos que el kernel expone pipes y
  sockets como si fueran archivos.
- **Clase 6 (señales, máscaras, async-signal-safe, self-pipe)**:
  `src/senales.py` implementa el patrón self-pipe completo: el handler
  real de `signal.signal()` sólo hace `os.write()` de un byte (la única
  operación que es segura de llamar dentro de un handler de señal sin
  arriesgar corromper estado del intérprete), y todo el trabajo real
  (terminar hijos, recargar `config.json`, volcar el snapshot) se hace
  después, fuera del contexto de señal, en `senales.procesar()`. También
  decodificamos las máscaras hexadecimales de 64 bits de `SigBlk` /
  `SigIgn` / `SigCgt` / `SigPnd` / `ShdPnd` bit a bit para mostrar nombres
  legibles de señal.
- **Clase 7 (mmap y memoria compartida)**: el `Array('d', 4)` compartido
  (`cpu_quick`) es memoria compartida real entre el analizador Sistema y
  el Display, sin pasar por el proceso servidor del Manager — la
  contraparte, en `multiprocessing`, de lo que en la clase vimos como
  `mmap(MAP_SHARED)` a nivel de syscall.
- **Clases 8-9 (multiprocessing, Queue/Pipe/Manager/Value/Array)**: toda
  la arquitectura: 9 procesos corriendo en paralelo (recolector + 7
  analizadores + el proceso servidor del Manager, sin contar el
  principal), comunicados con las cuatro primitivas que pide el
  enunciado.
- **Clase 10 (threading, GIL, threads como LWPs)**: `analizadores/threads.py`
  lee `/proc/<pid>/task/<tid>/...` para cada LWP del proceso — el TID de
  cada thread es justamente lo que vimos en clase que el kernel de Linux
  trata como un "proceso liviano" más, con su propio `/proc/<pid>/task/<tid>`.
  El propio Display usa `curses.nodelay()` en vez de un thread aparte
  para el teclado (el enunciado lo deja como opcional), así que la única
  concurrencia con threads del TP es el hilo despachador de señales en
  `main.py`, que no hace trabajo pesado, sólo reacciona al self-pipe.

## Preguntas que sabemos que nos van a hacer (y cómo las responderíamos)

- *"¿Por qué tu agregador usa `Manager.dict` y no un dict regular?"* — un
  dict regular vive en la memoria privada de un solo proceso; los otros 7
  procesos no podrían verlo ni modificarlo. `Manager.dict()` en cambio es
  un proxy hacia un dict que vive en un proceso servidor aparte (el
  Agregador), al que todos los analizadores y el Display le hacen RPC.
- *"Si matás uno de tus analizadores con `kill`, ¿qué pasa?"* — esa vista
  deja de actualizarse (el `ts` de `snapshot[clave]` queda congelado en el
  último valor), pero el resto del monitor sigue funcionando: cada
  analizador es un proceso `daemon=True` totalmente independiente, no hay
  ningún supervisor que reinicie analizadores caídos (ver Limitaciones).
- *"Si lanzás el monitor y abrís 100 terminales, ¿escala?"* — el costo
  principal es lineal en cantidad de procesos × cantidad de FDs/maps por
  proceso (la vista FDs es la más cara). Con 100 procesos livianos no
  debería notarse; con procesos que tengan miles de FDs cada uno, la
  vista FDs (intervalo 5s) empezaría a acumular latencia, que es
  justamente la razón por la que tiene el intervalo más alto de las 7.

## Limitaciones conocidas

- No hay reinicio automático de un analizador si muere o si tira una
  excepción no capturada por el `try/except` genérico de
  `correr_analizador` (que sólo protege la *función de cálculo*, no todo
  el proceso); si el proceso en sí muere (por ejemplo, `kill -9`), esa
  vista simplemente deja de refrescarse.
- Los `%CPU` (tanto por proceso como por thread) son sólo una
  aproximación: se calculan con el delta de `utime+stime` entre dos
  lecturas del mismo analizador, así que el primer valor que se ve para
  cada proceso siempre es `0.0` (todavía no hay lectura anterior con la
  que comparar), y la precisión depende del intervalo configurado para
  esa vista.
- Sin privilegios de root (o sin `docker run --privileged` / capacidades
  extra), la lectura de `/proc/<pid>/...` de procesos que no son propios
  del usuario puede fallar con `PermissionError`; el monitor lo maneja
  devolviendo `None`/valores vacíos para ese proceso en vez de romperse,
  pero esos procesos van a verse con datos incompletos.
- Dentro de Docker, por defecto el contenedor tiene su **propio**
  namespace de PIDs, así que el monitor sólo ve los procesos que corren
  adentro del contenedor (básicamente, a sí mismo). Para ver los procesos
  del host hace falta descomentar `pid: host` en `docker-compose.yml`
  (lo dejamos documentado ahí, pero no activado por defecto).
- La detección de la política de scheduling (`OTHER/FIFO/RR/...`) depende
  de poder leer `/proc/<pid>/sched`; en kernels o configuraciones donde
  ese archivo no está disponible, cae a `"?"`.
- `SIGWINCH` sólo fuerza un `stdscr.clear()` en el próximo frame; no
  recalcula layouts complejos más allá de lo que ya hace `curses` con el
  nuevo `getmaxyx()` en cada frame.
- No implementamos ninguna de las extensiones de bonus (histórico,
  detección de anomalías, modo daemon, pstree, comparativa cruzada), más
  allá de la vista Sistema mostrando el top 3 por CPU y por memoria.

## Decisiones sobre la TUI

Elegimos **`curses`** en vez de `rich` a propósito: es parte de la
biblioteca estándar de Python en Linux, así que `docker compose up --build`
no depende de tener acceso a red durante el build para bajar el paquete.
El layout es fijo (lista arriba, detalle abajo, separados por una línea de
guiones) y se recalcula en cada frame contra el tamaño real de la
terminal (`stdscr.getmaxyx()`), con `try/except curses.error` alrededor de
cada `addnstr` para no explotar si la terminal se hace más chica que el
contenido en el instante de dibujar (se recupera solo en el próximo
frame). El teclado se lee con `stdscr.nodelay(True)` + `timeout(200)` en
vez de bloquear, así el loop de dibujado nunca se cuelga esperando una
tecla — no hace falta un thread aparte para esto (a diferencia de lo que
sugiere el enunciado como opción), curses ya lo resuelve con esa llamada.

## Lo que aprendimos

Lo que más nos costó entender en la práctica, más allá de la teoría, fue
la diferencia real de costo entre las distintas primitivas de IPC: es una
cosa saber en el papel que `Manager.dict` hace RPC contra un proceso
servidor, y otra muy distinta notarlo cuando el Display se pone
perceptiblemente más lento a medida que agregábamos campos al snapshot, y
ver cómo sacar los 4 números de CPU global a un `Array` compartido lo
hacía sentir instantáneo en comparación.

También terminamos de entender por qué en clase insistieron tanto con que
los handlers de señal tienen que ser mínimos: la primera versión que
escribimos del manejo de señales hacía el trabajo (terminar procesos,
escribir archivos) directamente adentro del handler, y en algún momento,
mandando señales muy seguido, el monitor se quedaba en un estado raro. El
patrón self-pipe resolvió eso de raíz: el handler no toca nada más que un
`os.write()`, y todo el trabajo "de verdad" pasa a correr en un contexto
normal (el hilo despachador), donde sí es seguro tocar diccionarios,
abrir archivos, etc.

Por último, ver en la práctica que matar un analizador con `kill -9` no
tumba el resto del monitor (esa vista simplemente se queda congelada con
el último dato) nos hizo entender mejor por qué en sistemas reales se
prefiere un diseño de varios procesos chicos e independientes en vez de
un único proceso monolítico multi-threaded: un fallo queda contenido.
