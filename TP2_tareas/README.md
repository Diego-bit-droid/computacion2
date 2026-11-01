# TP2 — Plataforma de Tareas Distribuidas

Servicio HTTP al que se le encargan tareas (`descargar`, `hashear`,
`esperar`). Las ejecuta en forma concurrente con un pool de workers
asíncronos y permite consultar su estado, su resultado y cancelarlas.

## Uso

```bash
docker compose up --build
```

- `api` (puerto 8000): la plataforma. Docs interactivas en http://localhost:8000/docs
- `lento` (puerto 8001): servidor de prueba que responde con demoras conocidas,
  para probar `descargar` y medir sin depender de internet.

Configuración en `docker-compose.yml`: `WORKERS` (4), `PROCESOS_CPU` (2),
`COLA_MAX` (1000), `TIMEOUT_DEFECTO` (60 s), `GRACIA_APAGADO` (10 s).

Tests: `docker compose run --rm --no-deps api pytest -v`

### Ejemplos

```bash
# Encolar: responde 202 al instante con el id
curl -X POST localhost:8000/tareas -H 'content-type: application/json' \
     -d '{"tipo": "esperar", "segundos": 30}'

curl -X POST localhost:8000/tareas -H 'content-type: application/json' \
     -d '{"tipo": "descargar", "urls": ["http://lento:8001/demora/1", "https://example.com"]}'

curl -X POST localhost:8000/tareas -H 'content-type: application/json' \
     -d '{"tipo": "hashear", "texto": "hola", "iteraciones": 3000000}'

# Con timeout: a los 2 s pasa a "fallida"
curl -X POST localhost:8000/tareas -H 'content-type: application/json' \
     -d '{"tipo": "esperar", "segundos": 10, "timeout": 2}'

curl localhost:8000/tareas                      # listar
curl 'localhost:8000/tareas?estado=ejecutando'  # filtrar por estado
curl localhost:8000/tareas/<id>                 # estado y resultado
curl -X DELETE localhost:8000/tareas/<id>       # cancelar
curl localhost:8000/estadisticas

docker compose stop api   # SIGTERM: apagado ordenado -> data/estado_<ts>.json
```

Estados: `pendiente`, `ejecutando`, `completada`, `fallida`, `cancelada`.

Bonus: tests con pytest-asyncio (12, la mayoría de cancelación) y métricas
Prometheus en `GET /metrics`.

## Arquitectura

```
Cliente ──HTTP──► FastAPI (api.py)
                    │ POST: pool.encolar() ──► asyncio.Queue (máx COLA_MAX)
                    │ DELETE: pool.cancelar()          │
                    │ GET: lee pool.tareas             ▼
                    │                       N workers (corrutinas, pool.py)
                    │                                  │ una Task por tarea,
                    │                                  │ con asyncio.timeout
                    │                                  ▼
                    │                ┌─ descargar: httpx.AsyncClient (sockets)
                    │                ├─ esperar:   asyncio.sleep
                    │                └─ hashear:   ProcessPoolExecutor
                    ▼
           Al apagar: esperar tareas en curso ──► estado_<ts>.json
```

Todo corre en un solo proceso con un solo event loop, salvo el cálculo de
`hashear`, que va a procesos hijos.

**Flujo de una tarea:** el POST valida el cuerpo, crea la tarea como
`pendiente`, la pone en la cola con `put_nowait` y responde 202, sin esperar
la tarea. Un worker libre la saca de la cola, la pasa a `ejecutando` y la
corre en una `asyncio.Task` propia. Según cómo termine, queda `completada`,
`fallida` (por excepción o timeout) o `cancelada`.

| Archivo | Contenido |
|---|---|
| `src/main.py` | Arranque de uvicorn |
| `src/api.py` | Endpoints; crea y cierra el pool, el cliente httpx y el executor |
| `src/pool.py` | Cola, workers, cancelación, timeouts, apagado, estadísticas |
| `src/tareas.py` | Las tres tareas |
| `src/modelos.py` | `Tarea`, estados y validación del POST |
| `src/persistencia.py` | Volcado a JSON |
| `bench/` | Scripts de las mediciones |

## Decisiones de diseño

**¿Por qué `asyncio.Queue` y no `queue.Queue`?** `queue.Queue.get()` bloquea
el hilo. Como hay un solo hilo, congelaría el event loop entero.
`asyncio.Queue.get()` es una corrutina: si la cola está vacía, el worker se
suspende y el loop sigue atendiendo la API. La cola es acotada: si está
llena, el POST responde 503 en vez de crecer sin límite.

**¿Cómo evito que una tarea CPU-bound congele la API?** El event loop sólo
cambia de corrutina en un `await`, y un `for` que calcula hashes no tiene
ninguno. Por eso `hashear` manda el cálculo a un `ProcessPoolExecutor` con
`run_in_executor`: el loop hace `await` del resultado y queda libre mientras
otro proceso calcula. Uso procesos y no threads porque el cálculo es Python
puro, y por el GIL dos threads no ejecutan Python al mismo tiempo. El trabajo
va en bloques de 100.000 iteraciones: así hay un `await` entre bloque y
bloque donde la cancelación puede actuar.

**¿Cómo implementé la cancelación y qué pasa con los recursos?** Cada tarea
corre en su propia `asyncio.Task`, separada de la del worker. Si la tarea
está `pendiente`, el DELETE la marca `cancelada` y el worker la saltea al
sacarla de la cola. Si está `ejecutando`, el DELETE marca
`cancelada_por_cliente` y llama a `.cancel()` sobre su Task. Eso lanza
`CancelledError` en el `await` donde esté parada la tarea. La excepción sube
pasando por los `async with`, que liberan los recursos:

- en una descarga, `async with http.stream(...)` cierra la respuesta y
  libera la conexión, y el `TaskGroup` cancela las demás descargas de esa
  tarea y espera a que terminen;
- en `hashear`, el proceso termina el bloque que estaba calculando y el
  resultado se descarta.

El worker recibe la excepción, marca la tarea `cancelada` y sigue con la
próxima. El timeout funciona igual: `asyncio.timeout()` cancela la tarea al
vencer y el worker la marca `fallida`.

**¿Qué pasa si un worker lanza una excepción no capturada?** Las excepciones
de una tarea las atrapa el worker con `except Exception`: la tarea queda
`fallida` con el error y el worker sigue. Si el worker mismo se rompiera por
un bug, un callback lo detecta, lo loguea y lanza un worker nuevo, para que
el pool no pierda capacidad en silencio.

**Apagado:** uvicorn atiende SIGTERM y SIGINT: deja de aceptar conexiones y
ejecuta el cierre del `lifespan`. Ahí el pool espera hasta `GRACIA_APAGADO`
segundos a las tareas en curso, cancela las que sigan y vuelca todo a
`estado_<timestamp>.json`. Al arrancar, si hay un volcado previo, se reporta
por log.

No instalé handlers propios con `loop.add_signal_handler` (clase 20) porque
uvicorn ya instala los suyos con el mismo fin y los pisaría; uso su apagado y
pongo mi lógica en el `lifespan`.

Tampoco usé el centinela de la clase 21 para parar los workers. El centinela
entra al final de la cola, así que los workers procesarían todas las tareas
pendientes antes de apagarse, sin límite de tiempo. La consigna pide dejar de
tomar tareas nuevas y esperar *con límite* a las que están corriendo. Por eso
los workers dejan de empezar tareas nuevas, `asyncio.wait(..., timeout=GRACIA_APAGADO)`
espera a las que corren, y las que no terminan se cancelan. Las pendientes
quedan como `pendiente` en el volcado.

**httpx:** cliente HTTP asíncrono con una API parecida a `requests`. Un único
cliente compartido reutiliza conexiones (keep-alive) y limita el total a 100.
Dentro de cada tarea, un `asyncio.Semaphore(20)` limita las descargas
simultáneas.

## Mediciones

En mi máquina (Ryzen 7 3700U, 4 núcleos / 8 hilos, Docker en Fedora).
Scripts en `bench/`, se corren con `docker compose exec api python bench/<script>`.

### 1. Concurrencia I/O (`medir_io.py`)

20 descargas del servidor `lento`, cada una entre 0,2 y 0,8 s. Promedio de
3 corridas:

| Modo | Tiempo |
|---|---|
| Secuencial (`for` con `await`) | 9,88 s |
| Concurrente (`TaskGroup`) | 0,87 s |
| **Speedup** | **11,3x** |

Contra internet (httpbin.org): 34,1 s contra 2,6 s (12,9x).

En secuencial cada descarga espera a que termine la anterior: el total es la
suma de todas. En concurrente salen todas juntas y el total es
aproximadamente el de la más lenta (0,8 s).

### 2. Event loop bloqueado (`medir_bloqueo.py`)

Un `hashear` de ~3 s mientras otro proceso hace `GET /tareas` cada 50 ms:

| Modo | GETs respondidos | Latencia máxima |
|---|---|---|
| Sin tarea | 37 en 2 s | 38 ms (mediana 5,4 ms) |
| **Sin executor** | **1 en 3 s** | **2999 ms** |
| Con executor | 55 en 3,2 s | 16 ms (mediana 4,8 ms) |

Sin executor, la API no respondió nada durante todo el cálculo: el único GET
tardó lo mismo que el hash. El event loop estaba ocupado en el `for` y no
podía atender ninguna request. Con executor, la latencia es la misma que sin
tarea.

### 3. Techo del pool (`medir_techo.py`)

K tareas `esperar` de 1 s enviadas de golpe, con 4 workers:

| K | Tiempo total | Espera en cola (prom.) | Tareas/s |
|---|---|---|---|
| 4 | 1,14 s | 0,00 s | 3,5 |
| 8 | 2,06 s | 0,49 s | 3,9 |
| 16 | 4,10 s | 1,48 s | 3,9 |
| 32 | 8,17 s | 3,45 s | 3,9 |

El límite es **workers / duración = 4 tareas/s**. Pasado ese punto, cada
tarea sigue tardando 1 s, pero el tiempo extra se va en esperar en la cola.

Con `hashear` y 2 procesos: 1 tarea tarda 1,54 s, 2 tardan 1,58 s y 4 tardan
3,17 s. El límite lo pone la cantidad de procesos (o de núcleos): más workers
no ayudan.

Con 10.000 tareas de golpe: entran 1004 (4 ejecutando + 1000 en cola), las
otras 8996 reciben 503 y la API sigue respondiendo.

## Conceptos del curso aplicados

- **Clase 11 (sincronización):** el pool es el productor-consumidor con
  buffer acotado de la clase. El POST produce, los workers consumen, y
  `maxsize` cumple el papel del buffer lleno.
- **Clase 10 (threading):** por el GIL, dos threads no ejecutan Python a la
  vez. Por eso `hashear` usa procesos y no `to_thread`.
- **Clase 17 (I/O multiplexing):** el event loop es un `epoll` que avisa qué
  socket está listo. Por eso las 20 descargas concurrentes tardan lo que la
  más lenta (medición 1).
- **Clase 18 (de `yield` a asyncio):** `await` cede el control al loop como
  el `yield` del scheduler cooperativo. Una corrutina sin `await` no lo cede
  nunca (medición 2). `esperar` usa `asyncio.sleep` y no `time.sleep`.
- **Clase 19 (HTTP y FastAPI):** los endpoints son `async def` y no hacen
  nada bloqueante. Pydantic valida el cuerpo y devuelve 422. Los errores se
  lanzan con `HTTPException` (404, 409, 503). El pool, la cola y el executor
  se crean una vez en el `lifespan`.
- **Clase 20 (asyncio en red):** un solo `httpx.AsyncClient` con timeout;
  `Semaphore` para limitar descargas; `asyncio.timeout`; `cancel()` inyecta
  `CancelledError` en el próximo `await` y la limpieza se hace en los
  `async with`. No hace falta Lock porque ninguna sección crítica del pool
  tiene un `await` adentro.
- **Clase 21 (asyncio avanzado):** `asyncio.Queue` con `maxsize` como
  backpressure (503 si está llena), `task_done()` en un `finally`,
  `TaskGroup` en vez de `gather` para no dejar descargas huérfanas, y
  `run_in_executor` con `ProcessPoolExecutor` para el cálculo. La medición 2
  repite el experimento de la clase.
- **Clase 22 (concurrent.futures):** `cancel()` no frena un future que ya
  arrancó, por eso el hash va en bloques. `hash_iterado` está al nivel del
  módulo para poder picklearse. Un `add_done_callback` vigila a los workers
  para que una excepción no pase en silencio.

## Limitaciones conocidas

- Las tareas terminadas nunca se borran de memoria.
- Una tarea cancelada mientras estaba en cola sigue ocupando lugar hasta que
  un worker la saca.
- Al reiniciar, el volcado se reporta pero no se recarga: las tareas
  pendientes se pierden.
- Una tarea con muchas URLs puede ocupar las 100 conexiones del cliente
  compartido.
- Una tarea `hashear` cancelada termina el bloque que estaba calculando
  (~0,1 s).
- Sin límite de tareas por cliente.

## Lo que aprendí

_Pendiente._
