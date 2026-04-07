import time
from functools import wraps


def retry(max_attempts=3, delay=1, exceptions=(Exception,)):
    """
    Decorador que reintenta ejecutar una función si falla.

    Parámetros:
    - max_attempts: cantidad máxima de intentos
    - delay: segundos de espera entre intentos
    - exceptions: tupla de excepciones a capturar
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for intento in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if intento == max_attempts:
                        print(f"Intento {intento}/{max_attempts} falló.")
                        raise
                    print(f"Intento {intento}/{max_attempts} falló: {e}. Esperando {delay}s...")
                    time.sleep(delay)

        return wrapper

    return decorator

if __name__ == "__main__":
    import random

    @retry(max_attempts=3, delay=1)
    def conectar():
        """Simula conexión a un servidor"""
        if random.random() < 0.7:
            raise ConnectionError("Servidor no disponible")
        return "Conectado exitosamente"

    try:
        resultado = conectar()
        print(resultado)
    except Exception:
        print("Falló después de varios intentos")