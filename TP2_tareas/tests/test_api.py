"""Tests de los endpoints HTTP, con la app real y su lifespan (TestClient)."""
from conftest import esperar_estado_http

TERMINALES = {"completada", "fallida", "cancelada"}


def test_post_responde_202_sin_esperar(cliente):
    r = cliente.post("/tareas", json={"tipo": "esperar", "segundos": 5})
    assert r.status_code == 202
    cuerpo = r.json()
    assert cuerpo["estado"] == "pendiente"
    assert r.headers["location"] == f"/tareas/{cuerpo['id']}"
    cliente.delete(f"/tareas/{cuerpo['id']}")


def test_post_tipo_desconocido_da_422(cliente):
    r = cliente.post("/tareas", json={"tipo": "nada"})
    assert r.status_code == 422


def test_post_valores_invalidos_dan_422(cliente):
    invalidos = [
        {"tipo": "hashear", "texto": "a", "iteraciones": 0},
        {"tipo": "esperar", "segundos": -1},
        {"tipo": "descargar", "urls": []},
        {"tipo": "esperar", "segundos": 1, "timeout": 0},
    ]
    for cuerpo in invalidos:
        assert cliente.post("/tareas", json=cuerpo).status_code == 422, cuerpo
    assert cliente.get("/estadisticas").json()["total"] == 0   # nada encolado


def test_get_tarea_inexistente_da_404(cliente):
    assert cliente.get("/tareas/no-existe").status_code == 404


def test_hashear_de_punta_a_punta(cliente):
    tid = cliente.post("/tareas", json={"tipo": "hashear", "texto": "hola",
                                        "iteraciones": 1000}).json()["id"]
    t = esperar_estado_http(cliente, tid, TERMINALES)
    assert t["estado"] == "completada"
    assert len(t["resultado"]["sha256"]) == 64


def test_listar_filtra_por_estado(cliente):
    ok = cliente.post("/tareas", json={"tipo": "esperar",
                                       "segundos": 0}).json()["id"]
    esperar_estado_http(cliente, ok, TERMINALES)
    largo = cliente.post("/tareas", json={"tipo": "esperar",
                                          "segundos": 30}).json()["id"]
    esperar_estado_http(cliente, largo, {"ejecutando"})

    ejecutando = cliente.get("/tareas", params={"estado": "ejecutando"}).json()
    assert [t["id"] for t in ejecutando] == [largo]
    completadas = cliente.get("/tareas", params={"estado": "completada"}).json()
    assert [t["id"] for t in completadas] == [ok]
    assert cliente.get("/tareas", params={"estado": "nada"}).status_code == 422
    cliente.delete(f"/tareas/{largo}")


def test_delete_cancela_en_ejecucion(cliente):
    tid = cliente.post("/tareas", json={"tipo": "esperar",
                                        "segundos": 30}).json()["id"]
    esperar_estado_http(cliente, tid, {"ejecutando"})
    r = cliente.delete(f"/tareas/{tid}")
    assert r.status_code == 200
    assert r.json()["estado"] == "cancelada"
    assert cliente.get("/estadisticas").json()["workers"]["ocupados"] == 0


def test_delete_terminada_da_409_e_inexistente_404(cliente):
    tid = cliente.post("/tareas", json={"tipo": "esperar",
                                        "segundos": 0}).json()["id"]
    esperar_estado_http(cliente, tid, TERMINALES)
    assert cliente.delete(f"/tareas/{tid}").status_code == 409
    assert cliente.delete("/tareas/no-existe").status_code == 404


def test_timeout_por_tarea(cliente):
    tid = cliente.post("/tareas", json={"tipo": "esperar", "segundos": 10,
                                        "timeout": 0.2}).json()["id"]
    t = esperar_estado_http(cliente, tid, TERMINALES)
    assert t["estado"] == "fallida"
    assert "timeout" in t["error"]


def test_estadisticas_y_metricas(cliente):
    tid = cliente.post("/tareas", json={"tipo": "esperar",
                                        "segundos": 0}).json()["id"]
    esperar_estado_http(cliente, tid, TERMINALES)
    est = cliente.get("/estadisticas").json()
    assert est["por_estado"]["completada"] == 1
    assert est["workers"] == {"total": 2, "ocupados": 0}
    assert est["tiempo_promedio_ejecucion"] is not None

    metricas = cliente.get("/metrics").text
    assert 'tareas{estado="completada"} 1' in metricas
    assert "workers_total 2" in metricas
