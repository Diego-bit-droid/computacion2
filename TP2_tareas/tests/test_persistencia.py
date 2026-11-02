"""Tests del volcado de estado y del aviso de volcado previo."""
import json
import os

from fastapi.testclient import TestClient

import persistencia
from api import app


def test_sin_volcado_previo_devuelve_none(tmp_path):
    assert persistencia.reportar_volcado_previo(str(tmp_path)) is None


def test_reporta_el_volcado_mas_nuevo(tmp_path):
    viejo = tmp_path / "estado_100.json"
    nuevo = tmp_path / "estado_200.json"
    for ruta, mtime in [(viejo, 100), (nuevo, 200)]:
        ruta.write_text(json.dumps({"tareas": []}))
        os.utime(ruta, (mtime, mtime))
    assert persistencia.reportar_volcado_previo(str(tmp_path)) == str(nuevo)


def test_apagar_la_app_vuelca_las_tareas(tmp_path, monkeypatch):
    monkeypatch.setenv("DIR_ESTADO", str(tmp_path))
    monkeypatch.setenv("WORKERS", "1")
    monkeypatch.setenv("PROCESOS_CPU", "1")
    monkeypatch.setenv("GRACIA_APAGADO", "0.1")
    with TestClient(app) as cliente:
        # una que corre (se cancela al apagar) y otra que queda en cola
        cliente.post("/tareas", json={"tipo": "esperar", "segundos": 30})
        cliente.post("/tareas", json={"tipo": "esperar", "segundos": 30})
    # al salir del with corre el final del lifespan: espera, cancela y vuelca

    volcados = list(tmp_path.glob("estado_*.json"))
    assert len(volcados) == 1
    datos = json.loads(volcados[0].read_text())
    estados = sorted(t["estado"] for t in datos["tareas"])
    assert estados == ["cancelada", "pendiente"]
