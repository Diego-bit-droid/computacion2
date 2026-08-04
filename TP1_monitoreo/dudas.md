# Dudas

Cosas que nos quedaron dando vueltas mientras hacíamos el TP, por si sirve
de honestidad intelectual más que nada:

1. **`Value` para el intervalo: ¿hacía falta el `Lock` explícito?** Leímos
   que en CPython, escribir un `double` de 8 bytes alineado ya es atómico
   a nivel de la arquitectura x86-64, así que probablemente el
   `with val.get_lock():` que usamos en `display.py` al ajustar
   intervalos sea redundante en la práctica. Lo dejamos igual porque no
   estábamos 100% seguros de que esa garantía se sostenga en otras
   arquitecturas (ARM, por ejemplo, que es donde corre bastante gente con
   Apple Silicon vía Docker), y el enunciado menciona `linux/arm64` como
   plataforma válida.

2. **`multiprocessing.Manager` como "Agregador": ¿es trampa?** El
   enunciado pide un componente Agregador separado que "mantiene el
   snapshot global en memoria compartida". Nosotros no escribimos un
   proceso nuestro para eso: usamos el hecho de que `Manager()` ya levanta
   su propio proceso servidor por debajo. Nos parece una lectura válida
   (technically un proceso aparte, y lo mostramos con `ps -ef` corriendo),
   pero no estamos seguros de si la cátedra esperaba que escribiéramos
   nosotros mismos el loop de un proceso "Agregador" que reciba de los
   analizadores por otra vía (¿Queue?) y arme el dict a mano, en vez de
   apoyarnos en el Manager para las dos cosas (agregación + memoria
   compartida) a la vez.

3. **Precisión del `%CPU` con intervalos tan distintos entre vistas.**
   `resumen.py` calcula su propio delta de CPU con su propio intervalo
   (2s por default), y `sistema.py` calcula el suyo por separado con el
   suyo (también 2s, pero ajustable independientemente). Si alguien pone
   el intervalo de una vista en 0.5s y el de la otra en 10s, los dos
   "%CPU" que se pueden llegar a comparar (top 3 de la vista Sistema vs.
   el número que aparece en la lista de Resumen) van a estar promediados
   sobre ventanas de tiempo distintas y van a diferir. No nos pareció
   grave (cada uno mide lo suyo, con su propia ventana), pero es una
   inconsistencia visual que un usuario atento va a notar si tiene las
   dos vistas abiertas en momentos distintos y compara números.

4. **Zombies y `contar_fds` / `leer_status`.** Un proceso zombie no tiene
   `/proc/<pid>/fd` (ya liberó sus file descriptors), así que en la vista
   FDs simplemente no aparece (lo filtramos porque `contar_fds` devuelve
   0). ¿Está bien que un zombie "desaparezca" de esa vista en particular
   aunque siga apareciendo en Resumen y en Sistema (como zombie contado)?
   Nos pareció razonable pero es una asimetría entre vistas que no
   resolvimos de forma más elegante (por ejemplo, mostrándolo igual con
   una fila que diga explícitamente "sin FDs, proceso zombie").

5. **`SIGWINCH` dentro de Docker con `tty: true`.** Nos funcionó
   redimensionando la ventana de la terminal del host cuando corrimos el
   monitor localmente con `python3 src/main.py`, pero no llegamos a
   probar a fondo si el evento de resize se propaga igual de bien cuando
   el terminal "real" está afuera del contenedor y uno lo adjunta con
   `docker attach` o `docker compose up` sin detach. Si no se propaga,
   el layout se recalcula solo de todas formas en el próximo frame
   normal (porque leemos `getmaxyx()` en cada vuelta del loop), así que
   en el peor caso el único síntoma sería un frame de más antes de
   ajustarse, pero no llegamos a verificarlo en ese escenario puntual.
