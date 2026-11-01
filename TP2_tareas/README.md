# TP2 - Plataforma de Tareas Distribuidas

API HTTP del Trabajo Práctico Nº 2 de Computación II. El sistema acepta tareas
asíncronas de descarga, hash y espera, las ejecuta con un pool de workers y
permite consultar su estado y resultado.

## Arquitectura

```
+----------+      +------------------------+      +----------------------+
| Cliente  | ---> | FastAPI (api.py)       | ---> | pool.tareas (dict)   |
+----------+      | POST/GET/DELETE tareas |      | estado y resultados  |
                  +-----------+------------+      +----------+-----------+
                              | put_nowait(id)               ^
                              v                              |
                  +-----------+------------+                 |
                  | asyncio.Queue          |                 |
                  | (maxsize = COLA_MAX)   |                 |
                  +-----------+------------+                 |
                              | await get()                  |
                              v                              |
                  +-----------+------------+                 |
                  | Pool (N workers)       |-----------------+
                  | una Task por tarea,    |
                  | con asyncio.timeout    |
                  +---+--------+--------+--+
                      |        |        |
          +-----------+        |        +-------------+
          v                    v                      v
+-------------------+ +------------------+ +----------------------+
| descargar         | | esperar          | | hashear              |
| httpx.AsyncClient | | asyncio.sleep    | | ProcessPoolExecutor  |
| TaskGroup         | |                  | | (por bloques)        |
+-------------------+ +------------------+ +----------------------+
```

`POST /tareas` valida el cuerpo, guarda la tarea como `pendiente`, encola su
ID y responde 202 sin esperar la ejecución. Un worker libre saca el ID de la
cola, pasa la tarea a `ejecutando` y la corre en una `asyncio.Task` propia.
Según cómo termine, la marca `completada`, `fallida` o `cancelada`. Los
endpoints de consulta leen el mismo diccionario.

| Archivo | Contenido |
|---|---|
| `src/main.py` | Arranque de uvicorn |
| `src/api.py` | Endpoints; crea y cierra el pool, el cliente httpx y el executor |
| `src/pool.py` | Cola, workers, cancelación, timeouts, apagado, estadísticas |
| `src/tareas.py` | Las tres tareas |
| `src/modelos.py` | `Tarea`, estados y validación del POST |
| `src/persistencia.py` | Volcado a JSON y aviso del volcado previo |
| `src/servidor_lento.py` | Servidor de prueba con demoras conocidas |
| `bench/` | Scripts de las tres mediciones |
| `tests/` | Tests con pytest-asyncio |

## Ejecutar con Docker Compose

Desde este directorio:

```bash
docker compose up --build
```

Levanta dos servicios:

- `api`, en http://localhost:8000. La documentación interactiva de FastAPI
  está en http://localhost:8000/docs.
- `lento`, en http://localhost:8001: un servidor de prueba que responde con
  demoras conocidas (`/demora/N`, entre 0,2 y 0,8 s). Sirve para probar
  `descargar` y para medir sin depender de internet. Desde la API se lo
  alcanza como `http://lento:8001`.

```bash
curl http://localhost:8000/estadisticas
```

El servicio monta `./data` del host, donde quedan los volcados de estado.
Para detenerlo ordenadamente, usá Ctrl+C en la terminal de Compose o, desde
otra terminal:

```bash
docker compose stop
```

`docker compose stop` envía SIGTERM al contenedor.

Configuración (variables de entorno en `docker-compose.yml`):

| Variable | Default | Qué controla |
|---|---|---|
| `WORKERS` | 4 | Workers (corrutinas) que consumen la cola |
| `PROCESOS_CPU` | 2 | Procesos del `ProcessPoolExecutor` para `hashear` |
| `COLA_MAX` | 1000 | Máximo de tareas pendientes |
| `TIMEOUT_DEFECTO` | 60 | Timeout de una tarea, en segundos, si el POST no manda uno |
| `GRACIA_APAGADO` | 10 | Segundos que se espera a las tareas en curso al apagar |
| `DIR_ESTADO` | `/app/data` | Dónde se guardan los `estado_<timestamp>.json` |

Tests (bonus, pytest-asyncio):

```bash
docker compose run --rm --no-deps api pytest -v
```

## Ejecutar localmente

Desde este directorio, con Python 3.11 o superior:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/main.py
```

Fuera del contenedor, `PROCESOS_CPU` toma por defecto la cantidad de núcleos
de la máquina y los volcados van a `./data`. Para usar el servidor de prueba,
levantalo aparte con `python src/servidor_lento.py` y usá
`http://localhost:8001/...` en las URLs.

## Enviar tareas

`POST /tareas` responde `202 Accepted` al aceptar una tarea, sin esperar su
ejecución. El campo `tipo` indica qué formato tiene el resto del cuerpo:

```bash
# Esperar dos segundos
curl -i -X POST http://localhost:8000/tareas \
  -H 'Content-Type: application/json' \
  -d '{"tipo":"esperar","segundos":2}'

# Calcular SHA-256 iterado
curl -i -X POST http://localhost:8000/tareas \
  -H 'Content-Type: application/json' \
  -d '{"tipo":"hashear","texto":"hola","iteraciones":1000000}'

# Descargar varias URLs
curl -i -X POST http://localhost:8000/tareas \
  -H 'Content-Type: application/json' \
  -d '{"tipo":"descargar","urls":["http://lento:8001/demora/1","https://example.com"]}'

# Con timeout propio: a los 2 s pasa a "fallida"
curl -i -X POST http://localhost:8000/tareas \
  -H 'Content-Type: application/json' \
  -d '{"tipo":"esperar","segundos":10,"timeout":2}'
```

Reglas de validación:
- `iteraciones` tiene que ser mayor que cero.
- `segundos` tiene que ser cero o mayor.
- `urls` lleva entre 1 y 500 URLs HTTP o HTTPS.
- `timeout`, si se manda, tiene que ser mayor que cero y como máximo 3600.

Un tipo desconocido, un campo requerido ausente o un valor fuera de rango
responde 422 y no encola nada. Los campos que no corresponden al tipo se
ignoran.

Una respuesta aceptada tiene esta forma (con header
`Location: /tareas/<id>`):

```json
{"id": "5777b4e0f102", "estado": "pendiente", "timeout": 60.0}
```

Cuando la tarea termina, `resultado` depende del tipo:

- `esperar`: `{"durmio": 2.0003}`.
- `hashear`: `{"modo": "proceso", "iteraciones": 1000, "sha256": "<hex>", "segundos": 0.0052}`.
- `descargar`: el tiempo total, la suma de los tiempos individuales, la más
  lenta, los bytes y la cantidad de errores, más una lista `urls` con
  `url`, `estado_http`, `bytes` y `segundos` por cada URL. Si una URL falla,
  su entrada lleva `error` en vez de `estado_http` y las demás no se
  pierden. La tarea sólo queda `fallida` si fallan todas.

Dos parámetros existen sólo para las mediciones: `"modo":"secuencial"` en
`descargar` (en vez de concurrente) y `"modo":"bloqueante"` en `hashear` (sin
executor, congela el event loop a propósito).

## Consultar tareas

```bash
# Listar (las más nuevas primero) o filtrar por estado
curl 'http://localhost:8000/tareas'
curl 'http://localhost:8000/tareas?estado=pendiente'
curl 'http://localhost:8000/tareas?limite=10'

# Consultar una tarea por ID
curl 'http://localhost:8000/tareas/<id>'
```

Los estados posibles son `pendiente`, `ejecutando`, `completada`, `fallida` y
`cancelada`. El detalle incluye los parámetros, los tiempos (`creada`,
`iniciada`, `finalizada`, `duracion`), el worker, el `progreso` (de 0 a 1) y
el `resultado` o el `error`. El listado no incluye el resultado y devuelve
100 tareas por defecto (`limite`, máximo 10.000).

| Endpoint | Respuesta esperada | Errores principales |
|---|---|---|
| `POST /tareas` | 202, ID y estado `pendiente` | 422 si el tipo o sus campos no son válidos; 503 si la cola está llena o el servidor se está apagando |
| `GET /tareas` | 200, lista (opcionalmente filtrada por estado) | 422 si el filtro no es un estado válido |
| `GET /tareas/{id}` | 200, estado y resultado/error | 404 si el ID no existe |
| `DELETE /tareas/{id}` | 200, la tarea cancelada | 404 si el ID no existe; 409 si ya terminó |
| `GET /estadisticas` | 200, totales por estado, tiempos promedio y workers ocupados | — |
| `GET /metrics` | 200, las mismas métricas en formato Prometheus (bonus) | — |

## Cancelar tareas

```bash
curl -i -X DELETE 'http://localhost:8000/tareas/<id>'
```

- **200**: la tarea fue cancelada. Si estaba en ejecución, el endpoint espera
  (hasta 2 s) a que termine la limpieza, así la respuesta ya dice
  `cancelada`.
- **404**: no existe una tarea con ese ID.
- **409**: la tarea ya terminó y no se modifica; el mensaje informa el estado
  actual.

## Estadísticas

```bash
curl 'http://localhost:8000/estadisticas'
```

La respuesta contiene:
- `por_estado`: totales de cada estado.
- `workers`: total y `ocupados`.
- `cola`: pendientes y capacidad.
- `tiempo_promedio_ejecucion`, en general y por tipo, y
  `tiempo_promedio_en_cola` (`null` si todavía no hay datos).

## Límite de la cola

La cola tiene capacidad `COLA_MAX` (default 1000). Si está llena,
`POST /tareas` responde **503** sin crear la tarea, en vez de esperar lugar o
crecer sin límite. Con 10.000 tareas enviadas de golpe entraron 1004 (4
ejecutando + 1000 en cola), las otras 8996 recibieron 503 y la API siguió
respondiendo.

## Apagado y volcado de estado

uvicorn atiende SIGTERM y SIGINT: deja de aceptar conexiones y ejecuta el
cierre del `lifespan` (`api.py`). Ahí el pool:

1. Deja de empezar tareas nuevas.
2. Espera hasta `GRACIA_APAGADO` segundos a las que están en ejecución.
3. Cancela las que no terminaron (quedan `cancelada por apagado del
   servidor`).
4. Vuelca todas las tareas a `estado_<timestamp>.json` en `DIR_ESTADO`. Las
   que no llegaron a empezar quedan como `pendiente`.

Al iniciar, el servicio informa por log el volcado más reciente, pero no
restaura ni vuelve a ejecutar esas tareas.

No instalé handlers propios con `loop.add_signal_handler` (clase 20) porque
uvicorn instala los suyos con el mismo fin y los pisaría. Tampoco usé el
centinela de la clase 21 para parar los workers: el centinela entra al final
de la cola, así que se procesarían todas las pendientes antes de apagar, sin
límite de tiempo, y la consigna pide esperar *con límite*.

## Decisiones de diseño

**¿Por qué `asyncio.Queue` y no `queue.Queue`?** `queue.Queue.get()` bloquea
el hilo, y como hay un solo hilo congelaría el event loop entero.
`asyncio.Queue.get()` es una corrutina: si la cola está vacía, el worker se
suspende y el loop sigue atendiendo la API.

**¿Cómo evito que una tarea CPU-bound congele la API?** El event loop sólo
cambia de corrutina en un `await`, y un `for` que calcula hashes no tiene
ninguno. Por eso `hashear` manda el cálculo a un `ProcessPoolExecutor` con
`run_in_executor`: el loop espera el resultado con `await` y queda libre
mientras otro proceso calcula. Uso procesos y no threads porque el cálculo es
Python puro, y por el GIL dos threads no ejecutan Python al mismo tiempo. El
trabajo va en bloques de 100.000 iteraciones, con un `await` entre bloques
donde la cancelación puede actuar.

**¿Cómo implementé la cancelación y qué pasa con los recursos a medio usar?**
Cada tarea corre en su propia `asyncio.Task`, separada de la del worker. Si
la tarea está `pendiente`, el DELETE la marca `cancelada` y el worker la
saltea al sacarla de la cola. Si está `ejecutando`, el DELETE marca
`cancelada_por_cliente` y llama a `.cancel()` sobre su Task. Eso lanza
`CancelledError` en el `await` donde esté parada la tarea, y la excepción
sube pasando por los `async with`, que liberan los recursos:

- en una descarga, `async with http.stream(...)` cierra la respuesta y
  libera la conexión, y el `TaskGroup` cancela las demás descargas de esa
  tarea y espera a que terminen;
- en `hashear`, el proceso termina el bloque que estaba calculando y el
  resultado se descarta.

El worker recibe la excepción, marca la tarea `cancelada` y sigue con la
próxima. El timeout funciona igual: `asyncio.timeout()` cancela la tarea al
vencer y el worker la marca `fallida` con el motivo.

**¿Qué pasa si un worker lanza una excepción no capturada?** Las excepciones
de una tarea las atrapa el worker con `except Exception`: la tarea queda
`fallida` con el error y el worker sigue con la siguiente. Si el worker mismo
se rompiera por un bug, un callback (`add_done_callback`) lo detecta, lo
loguea y lanza un worker nuevo, para que el pool no pierda capacidad en
silencio.

**httpx y no aiohttp:** las dos son asíncronas, pero httpx tiene la misma API
que `requests` (`get`, `status_code`, `content`), es la que usamos en la
clase 20, y `client.stream()` funciona como `async with`, lo que hace que una
cancelación a mitad de descarga cierre la conexión sola. Además trae
`MockTransport`, que permite testear `descargar` sin red. Un único cliente
compartido reutiliza conexiones (keep-alive) y limita el total a 100. Dentro
de cada tarea, un `asyncio.Semaphore(20)` limita las descargas simultáneas.

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
- **Clase 20 (asyncio en red):** un solo `httpx.AsyncClient` con timeout,
  `Semaphore` para limitar descargas y `asyncio.timeout`. `cancel()` inyecta
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

## Mediciones

Las mediciones se repiten con la API levantada (`docker compose up --build`)
desde otra terminal:

```bash
docker compose exec api python bench/medir_io.py
docker compose exec api python bench/medir_bloqueo.py --iteraciones 4000000
docker compose exec api python bench/medir_techo.py --tipo esperar
docker compose exec api python bench/medir_techo.py --tipo hashear --cargas 1,2,4
```

Entorno de esta ejecución: Docker (`python:3.12-slim`) sobre Fedora 43, AMD
Ryzen 7 3700U (4 núcleos / 8 hilos), con 4 workers (`WORKERS`) y 2 procesos
(`PROCESOS_CPU`). Los valores pueden variar entre ejecuciones y máquinas.

### Concurrencia I/O: 20 descargas

Las 20 descargas apuntaron al servidor `lento`, que tarda entre 0,2 y 0,8 s
por URL (0,8 s la más lenta). Así la comparación no depende de la
disponibilidad de sitios externos. Promedio de 3 corridas:

| Ejecución | Tiempo |
|---|---|
| Secuencial (`for` con `await`) | 9,88 s |
| Concurrente (`TaskGroup`) | 0,87 s |
| Speedup | 11,3× |

Contra internet (httpbin.org, con `--externas`): 34,1 s contra 2,6 s (12,9×).

En secuencial, cada descarga espera a que termine la anterior: el total es
la suma de todas. En concurrente salen todas juntas y el total es
aproximadamente el de la más lenta.

### Event loop durante CPU-bound

Un `hashear` de 4.000.000 de iteraciones (~3 s) mientras otro proceso hace
`GET /tareas` cada 50 ms y mide cuánto tarda cada respuesta:

| Modo | GETs respondidos | Latencia de `GET /tareas` |
|---|---|---|
| Sin tarea (base) | 37 en 2 s | mediana 5,4 ms, máximo 38 ms |
| Sin executor | 1 en 3 s | **2999 ms** |
| `ProcessPoolExecutor` | 55 en 3,2 s | mediana 4,8 ms, máximo 16 ms |

Sin executor, la API no respondió nada durante todo el cálculo: el único GET
tardó lo mismo que el hash, porque el event loop estaba ocupado en el `for`
y no podía atender ninguna request. Con executor, la latencia es la misma que
sin tarea.

### Techo del pool

Lotes crecientes de tareas `esperar` de 1 s, enviadas de golpe, con 4
workers:

| Tareas en el lote | Tiempo total | Espera en cola (prom.) | Throughput |
|---|---|---|---|
| 4 | 1,14 s | 0,00 s | 3,5 tareas/s |
| 8 | 2,06 s | 0,49 s | 3,9 tareas/s |
| 16 | 4,10 s | 1,48 s | 3,9 tareas/s |
| 32 | 8,17 s | 3,45 s | 3,9 tareas/s |

El límite es **workers / duración = 4 tareas/s**. Pasado ese punto, cada
tarea sigue tardando 1 s, pero el tiempo extra se va en esperar en la cola.

Con `hashear` (2.000.000 de iteraciones) y 2 procesos: 1 tarea tarda 1,54 s,
2 tardan 1,58 s y 4 tardan 3,17 s. En tareas CPU-bound el techo lo ponen los
procesos del executor (y los núcleos), no la cantidad de workers.

## Limitaciones conocidas

- Las tareas terminadas nunca se borran de memoria.
- Una tarea cancelada mientras estaba en cola sigue ocupando lugar hasta que
  un worker la saca.
- El JSON de apagado es una copia para inspección: al reiniciar no se recarga
  y las tareas pendientes se pierden.
- La cancelación de `hashear` espera a que termine el bloque que se está
  calculando en el proceso hijo (~0,1 s).
- Una tarea con muchas URLs puede ocupar las 100 conexiones del cliente
  compartido.
- No hay límite de tareas por cliente ni autenticación.

## Lo que aprendí

_Pendiente: completar personalmente antes de entregar (2-3 párrafos)._
