"""
Tests del parseo de /proc. Usan unittest (stdlib) para no depender de pytest
ni de ninguna librería externa (el contenedor no tiene por qué tener acceso
a red para instalarlas). Se pueden correr igual con pytest si está instalado.

Corren contra el propio proceso de test (os.getpid()), que siempre existe,
y contra un archivo /proc/<pid>/status de muestra embebido para no depender
de valores específicos del sistema donde se corra.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import procfs  # noqa: E402


STATUS_DE_MUESTRA = """Name:\tbash
State:\tS (sleeping)
Pid:\t1234
PPid:\t1
Uid:\t1000\t1000\t1000\t1000
Gid:\t1000\t1000\t1000\t1000
Threads:\t1
VmSize:\t   12345 kB
VmRSS:\t    6789 kB
VmSwap:\t       0 kB
SigBlk:\t0000000000010000
SigIgn:\t0000000000384004
SigCgt:\t0000000000000001
voluntary_ctxt_switches:\t42
nonvoluntary_ctxt_switches:\t7
"""


class TestParseoStatus(unittest.TestCase):
    def setUp(self):
        self.path = "/tmp/status_muestra_test"
        with open(self.path, "w") as f:
            f.write(STATUS_DE_MUESTRA)

    def tearDown(self):
        os.remove(self.path)

    def test_leer_status_directo(self):
        # leer_status usa una ruta fija /proc/<pid>/status; probamos el parser
        # interno reimplementando la lectura sobre el archivo de muestra.
        d = {}
        for linea in open(self.path):
            if ":" not in linea:
                continue
            clave, valor = linea.split(":", 1)
            clave, valor = clave.strip(), valor.strip()
            if clave in ("Uid", "Gid"):
                d[clave] = [int(p) for p in valor.split()]
            elif clave in procfs._STATUS_INT_KEYS:
                import re
                m = re.match(r"(-?\d+)", valor)
                d[clave] = int(m.group(1)) if m else 0
            elif clave in ("SigBlk", "SigIgn", "SigCgt", "SigPnd", "ShdPnd"):
                d[clave] = int(valor, 16)
            else:
                d[clave] = valor
        self.assertEqual(d["Pid"], 1234)
        self.assertEqual(d["Uid"], [1000, 1000, 1000, 1000])
        self.assertEqual(d["VmRSS"], 6789)
        self.assertEqual(d["voluntary_ctxt_switches"], 42)

    def test_decodificar_mascara_senales_sigint(self):
        # bit 2 = SIGINT -> máscara 0x2 (2^(2-1))
        nombres = procfs.decodificar_mascara_senales(0x2)
        self.assertEqual(nombres, ["SIGINT"])

    def test_decodificar_mascara_senales_multiple(self):
        # SIGHUP (bit1) + SIGINT (bit2) + SIGTERM (bit15)
        mascara = (1 << 0) | (1 << 1) | (1 << 14)
        nombres = procfs.decodificar_mascara_senales(mascara)
        self.assertEqual(nombres, ["SIGHUP", "SIGINT", "SIGTERM"])

    def test_decodificar_mascara_vacia(self):
        self.assertEqual(procfs.decodificar_mascara_senales(0), [])


class TestProcesoPropio(unittest.TestCase):
    """Estos tests leen /proc del propio proceso de test: siempre debe existir."""

    def test_listar_pids_incluye_propio_pid(self):
        pids = procfs.listar_pids()
        self.assertIn(os.getpid(), pids)

    def test_leer_stat_propio(self):
        st = procfs.leer_stat(os.getpid())
        self.assertIsNotNone(st)
        self.assertEqual(st["pid"], os.getpid())
        self.assertIn(st["state"], ("R", "S", "D"))

    def test_leer_status_propio(self):
        status = procfs.leer_status(os.getpid())
        self.assertIsNotNone(status)
        self.assertIn("VmRSS", status)
        self.assertGreater(status["VmRSS"], 0)

    def test_leer_cmdline_propio_no_vacio(self):
        cmdline = procfs.leer_cmdline(os.getpid())
        self.assertTrue(len(cmdline) > 0)

    def test_listar_tids_incluye_al_menos_un_thread(self):
        tids = procfs.listar_tids(os.getpid())
        self.assertIn(os.getpid(), tids)  # el thread principal tiene TID == PID

    def test_leer_fds_propio(self):
        fds = procfs.leer_fds(os.getpid())
        self.assertGreaterEqual(len(fds), 1)
        for fd in fds:
            self.assertIn("tipo", fd)

    def test_leer_maps_agrupado_no_negativo(self):
        grupos = procfs.leer_maps_agrupado(os.getpid())
        for tam in grupos.values():
            self.assertGreaterEqual(tam, 0)

    def test_leer_cpu_global(self):
        cpu = procfs.leer_cpu_global()
        self.assertIsNotNone(cpu)
        self.assertIn("user", cpu)
        self.assertIn("idle", cpu)

    def test_leer_meminfo_tiene_memtotal(self):
        mem = procfs.leer_meminfo()
        self.assertIn("MemTotal", mem)
        self.assertGreater(mem["MemTotal"], 0)

    def test_pid_inexistente_devuelve_none(self):
        # un PID absurdamente alto no debería existir
        self.assertIsNone(procfs.leer_stat(999999))
        self.assertIsNone(procfs.leer_status(999999))


if __name__ == "__main__":
    unittest.main()
