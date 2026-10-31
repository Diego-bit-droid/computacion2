"""
modelos.py
La Tarea (estado interno, en memoria) y los pedidos que acepta el POST.

La Tarea es un dataclass común y corriente: no tiene locks porque sólo la
modifica código que corre en el event loop (un único hilo). Entre dos `await`
nadie más puede tocarla, así que cada cambio de estado es atómico "gratis".
"""
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import AnyHttpUrl, BaseModel, Field


class Estado(str, Enum):
    PENDIENTE = "pendiente"
    EJECUTANDO = "ejecutando"
    COMPLETADA = "completada"
    FALLIDA = "fallida"
    CANCELADA = "cancelada"

    @property
    def terminal(self):
        return self in (Estado.COMPLETADA, Estado.FALLIDA, Estado.CANCELADA)


@dataclass
class Tarea:
    tipo: str
    parametros: dict
    timeout: float
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    estado: Estado = Estado.PENDIENTE
    creada: float = field(default_factory=time.time)
    iniciada: Optional[float] = None
    finalizada: Optional[float] = None
    worker: Optional[int] = None
    progreso: float = 0.0
    resultado: Optional[dict] = None
    error: Optional[str] = None
    # Lo pone el DELETE. Así el worker distingue "cancelaron la tarea" de
    # "me están cancelando a mí porque se apaga el servidor".
    cancelada_por_cliente: bool = False

    def duracion(self):
        if self.iniciada is None:
            return None
        fin = self.finalizada if self.finalizada is not None else time.time()
        return fin - self.iniciada

    def a_dict(self, con_resultado=True):
        d = {
            "id": self.id,
            "tipo": self.tipo,
            "estado": self.estado.value,
            "parametros": self.parametros,
            "timeout": self.timeout,
            "creada": self.creada,
            "iniciada": self.iniciada,
            "finalizada": self.finalizada,
            "duracion": self.duracion(),
            "worker": self.worker,
            "progreso": round(self.progreso, 3),
            "error": self.error,
        }
        if con_resultado:
            d["resultado"] = self.resultado
        return d


# --- pedidos (cuerpo del POST /tareas) --------------------------------------
# Unión discriminada por "tipo": pydantic elige el modelo según ese campo y
# FastAPI devuelve 422 solo, con el detalle, si algo no valida.

class _PedidoBase(BaseModel):
    timeout: Optional[float] = Field(None, gt=0, le=3600,
                                     description="segundos")


class PedidoDescargar(_PedidoBase):
    tipo: Literal["descargar"]
    urls: list[AnyHttpUrl] = Field(..., min_length=1, max_length=500)
    # "secuencial" existe para la medición 1 (for con await vs TaskGroup).
    modo: Literal["concurrente", "secuencial"] = "concurrente"


class PedidoHashear(_PedidoBase):
    tipo: Literal["hashear"]
    texto: str = Field(..., max_length=10_000)
    iteraciones: int = Field(..., ge=1, le=500_000_000)
    # "proceso" es el modo normal. "bloqueante" existe sólo para la
    # medición 2: congela el event loop A PROPÓSITO.
    modo: Literal["proceso", "bloqueante"] = "proceso"


class PedidoEsperar(_PedidoBase):
    tipo: Literal["esperar"]
    segundos: float = Field(..., ge=0, le=3600)
    fallar: bool = Field(False, description="lanza una excepción al terminar")


Pedido = Annotated[
    Union[PedidoDescargar, PedidoHashear, PedidoEsperar],
    Field(discriminator="tipo"),
]


def parametros_de(pedido):
    """Lo que guardamos en la Tarea: todo menos tipo y timeout, serializable."""
    return pedido.model_dump(mode="json", exclude={"tipo", "timeout"})
