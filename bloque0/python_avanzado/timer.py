import time

class Timer:
    def __init__(self, nombre=None):
        self.nombre = nombre
        self.start = None
        self.elapsed = 0

    def __enter__(self):
        self.start = time.time()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.elapsed = time.time() - self.start
        if self.nombre:
            print(f"[Timer] {self.nombre}: {self.elapsed:.3f}s")

if __name__ == "__main__":
    with Timer("Prueba de tiempo"):
        time.sleep(1)

    with Timer() as t:
        time.sleep(0.5)
    print(f"Tiempo medido: {t.elapsed:.3f}s")