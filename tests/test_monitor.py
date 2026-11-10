"""Tests de monitor.py. Solo usan la libreria estandar:

    python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Que los tests no lean el config.json personal (por ejemplo, modo fijo o pomodoro 50/10):
# siempre corren con los valores de fabrica, igual en cualquier PC y en GitHub.
os.environ["MONITOR_SIN_CONFIG"] = "1"
import monitor  # noqa: E402


class TestFormatos(unittest.TestCase):
    def test_fmt_bytes(self):
        self.assertEqual(monitor.fmt_bytes(512), "512 B")
        self.assertEqual(monitor.fmt_bytes(1536), "1.5 KB")
        self.assertEqual(monitor.fmt_bytes(3 * 1024 ** 3), "3.0 GB")

    def test_fmt_tiempo(self):
        self.assertEqual(monitor.fmt_tiempo(0), "0:00")
        self.assertEqual(monitor.fmt_tiempo(83.9), "1:23")

    def test_fmt_cuenta_redondea_para_arriba(self):
        # Con 59.2 s restantes se muestra 01:00, no 00:59 (como cualquier temporizador)
        self.assertEqual(monitor.fmt_cuenta(59.2), "01:00")
        self.assertEqual(monitor.fmt_cuenta(1500), "25:00")

    def test_fmt_uptime(self):
        self.assertEqual(monitor.fmt_uptime(3 * 3600 + 5 * 60), "3h 5m")
        self.assertEqual(monitor.fmt_uptime(2 * 86400 + 3600), "2d 1h 0m")

    def test_color_por_umbral(self):
        self.assertEqual(monitor.color(10), "#4caf50")
        self.assertEqual(monitor.color(70), "#ffb300")
        self.assertEqual(monitor.color(95), "#e53935")


class TestClima(unittest.TestCase):
    def test_tipo_icono(self):
        self.assertEqual(monitor.tipo_icono(0, True), "sol")
        self.assertEqual(monitor.tipo_icono(0, False), "luna")
        self.assertEqual(monitor.tipo_icono(2, True), "solnube")
        self.assertEqual(monitor.tipo_icono(63), "lluvia")
        self.assertEqual(monitor.tipo_icono(95), "tormenta")
        self.assertEqual(monitor.tipo_icono(45), "niebla")
        self.assertEqual(monitor.tipo_icono(73), "nieve")

    @unittest.skipIf(monitor.Image is None, "Pillow no instalado")
    def test_iconos_son_png(self):
        for tipo in ("sol", "luna", "solnube", "lunanube", "nube", "lluvia", "tormenta", "nieve", "niebla"):
            self.assertTrue(monitor.dibujar_icono(tipo, 48).startswith(b"\x89PNG"), tipo)


class TestPomodoro(unittest.TestCase):
    def setUp(self):
        monitor._pomo.update(fase="trabajo", corriendo=False, restante=monitor.POMO_TRABAJO, ciclos=0)
        self.notif = mock.patch.object(monitor, "notificar").start()
        self.addCleanup(mock.patch.stopall)

    def test_iniciar_y_pausar_conserva_el_tiempo(self):
        monitor.pomo_accion("iniciar")
        self.assertTrue(monitor._pomo["corriendo"])
        monitor._pomo["fin"] = time.time() + 100  # como si faltaran 100 s
        monitor.pomo_accion("pausar")
        self.assertFalse(monitor._pomo["corriendo"])
        self.assertAlmostEqual(monitor.pomo_restante(), 100, delta=1)

    def test_ciclo_completo_y_descanso_largo(self):
        fases = []
        for _ in range(monitor.POMO_CADA):
            with monitor._pomo_candado:
                monitor._pomo_pasar_fase(completada=True)   # termina trabajo
            fases.append(monitor._pomo["fase"])
            with monitor._pomo_candado:
                monitor._pomo_pasar_fase(completada=True)   # termina descanso
        self.assertEqual(fases, ["descanso"] * (monitor.POMO_CADA - 1) + ["largo"])
        self.assertEqual(monitor._pomo["ciclos"], monitor.POMO_CADA)
        self.assertTrue(self.notif.called)

    def test_saltar_no_cuenta_como_completado(self):
        monitor.pomo_accion("saltar")
        self.assertEqual(monitor._pomo["fase"], "descanso")
        self.assertEqual(monitor._pomo["ciclos"], 0)

    def test_reiniciar(self):
        monitor._pomo.update(fase="largo", ciclos=7, corriendo=True)
        monitor.pomo_accion("reiniciar")
        self.assertEqual((monitor._pomo["fase"], monitor._pomo["ciclos"], monitor._pomo["corriendo"]),
                         ("trabajo", 0, False))


class TestNotas(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        archivo = os.path.join(self.dir.name, "notas.json")
        mock.patch.object(monitor, "NOTAS_ARCHIVO", archivo).start()
        self.addCleanup(mock.patch.stopall)
        monitor._notas.clear()

    def test_agregar_guarda_en_disco(self):
        self.assertTrue(monitor.agregar_nota("  comprar leche  "))
        self.assertFalse(monitor.agregar_nota("   "))  # vacia: no se agrega
        with open(monitor.NOTAS_ARCHIVO, encoding="utf-8") as f:
            self.assertEqual([n["texto"] for n in json.load(f)], ["comprar leche"])

    def test_la_mas_nueva_va_primero_y_hay_tope(self):
        for i in range(monitor.NOTAS_MAX + 5):
            monitor.agregar_nota(f"nota {i}")
        self.assertEqual(len(monitor._notas), monitor.NOTAS_MAX)
        self.assertEqual(monitor._notas[0]["texto"], f"nota {monitor.NOTAS_MAX + 4}")

    def test_borrar(self):
        monitor.agregar_nota("a")
        monitor.agregar_nota("b")
        monitor.borrar_nota(monitor._notas[0]["id"])
        self.assertEqual([n["texto"] for n in monitor._notas], ["a"])
        monitor.borrar_nota("todas")
        self.assertEqual(monitor._notas, [])

    def test_html_se_escapa(self):
        monitor.agregar_nota("<script>alert(1)</script>")
        _, _, _, secciones = monitor.generar_notas()
        self.assertNotIn("<script>alert", secciones[1])
        self.assertIn("&lt;script&gt;", secciones[1])

    def test_tamano_de_letra(self):
        self.assertGreater(monitor.tam_nota(1, 10), monitor.tam_nota(1, 150))
        self.assertGreater(monitor.tam_nota(1, 10), monitor.tam_nota(5, 10))


class TestRotacion(unittest.TestCase):
    def setUp(self):
        mock.patch.object(monitor, "_notas", []).start()
        self.addCleanup(mock.patch.stopall)
        monitor._pomo["corriendo"] = False
        monitor._ultimo_sonando = 0.0

    def test_pomodoro_tiene_prioridad(self):
        monitor._pomo["corriendo"] = True
        monitor._ultimo_sonando = time.time()
        self.assertEqual(monitor.pagina_sugerida(), "/pomodoro")
        monitor._pomo["corriendo"] = False

    def test_musica_reciente(self):
        monitor._ultimo_sonando = time.time() - 10
        self.assertEqual(monitor.pagina_sugerida(), "/musica")

    def test_sin_musica_alterna(self):
        monitor._ultimo_sonando = -1e9  # hace muchisimo que no suena nada
        with mock.patch.object(monitor.time, "time", return_value=0.0):
            primera = monitor.pagina_sugerida()
        with mock.patch.object(monitor.time, "time", return_value=float(monitor.ROTACION_SEG)):
            segunda = monitor.pagina_sugerida()
        self.assertEqual([primera, segunda], monitor.ROTAR_SIN_MUSICA[:2])

    def test_orden_del_panel_se_usa_una_vez(self):
        monitor.dar_orden("/clima?manual=1")
        self.assertEqual(monitor.tomar_orden("1.2.3.4"), "/clima?manual=1")
        self.assertIsNone(monitor.tomar_orden("1.2.3.4"))

    def test_modo_fijo_no_sugiere_nada(self):
        mock.patch.dict(monitor._rotacion, {"activa": False}).start()
        monitor._pomo["corriendo"] = True        # ni el pomodoro mueve la tablet
        monitor._ultimo_sonando = time.time()    # ni la musica
        self.assertEqual(monitor.pagina_sugerida(), "")

    def test_modo_fijo_igual_obedece_al_panel(self):
        mock.patch.dict(monitor._rotacion, {"activa": False}).start()
        monitor.dar_orden("/notas?manual=1")
        self.assertEqual(monitor.tomar_orden("1.2.3.4"), "/notas?manual=1")
        monitor.dar_orden("auto")                # "automatico" en modo fijo: no hace nada
        self.assertIsNone(monitor.tomar_orden("1.2.3.4"))


class TestClaude(unittest.TestCase):
    """Uso de Claude: lectura de la sesion, parseo, proyeccion y pagina."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cred = os.path.join(self.dir.name, ".credentials.json")
        mock.patch.object(monitor, "CLAUDE_CREDENCIALES", self.cred).start()
        mock.patch.dict(monitor._claude, {"datos": None, "actualizado": 0.0, "error": None}).start()
        self.addCleanup(mock.patch.stopall)

    def escribir_cred(self, **oauth):
        with open(self.cred, "w") as f:
            json.dump({"claudeAiOauth": oauth}, f)

    def test_sin_sesion(self):
        with self.assertRaises(monitor.ErrorClaude):
            monitor.leer_token_claude()

    def test_sesion_vencida(self):
        self.escribir_cred(accessToken="secreto", expiresAt=(time.time() - 60) * 1000)
        with self.assertRaises(monitor.ErrorClaude):
            monitor.leer_token_claude()

    def test_sesion_valida(self):
        self.escribir_cred(accessToken="secreto", expiresAt=(time.time() + 3600) * 1000)
        self.assertEqual(monitor.leer_token_claude(), "secreto")

    def test_a_epoch(self):
        self.assertEqual(monitor.a_epoch("1970-01-01T00:01:00Z"), 60)
        self.assertEqual(monitor.a_epoch("1970-01-01T00:01:00+00:00"), 60)
        self.assertIsNone(monitor.a_epoch("cualquier cosa"))
        self.assertIsNone(monitor.a_epoch(None))

    def test_ventanas_ignora_las_vacias(self):
        datos = {"five_hour": {"utilization": 17.0, "resets_at": "2099-01-01T00:00:00Z"},
                 "seven_day": {"utilization": 25, "resets_at": None},
                 "seven_day_opus": None, "extra_usage": {"is_enabled": False}}
        v = monitor.ventanas_claude(datos)
        self.assertEqual([x["clave"] for x in v], ["five_hour", "seven_day"])
        self.assertIsNone(v[1]["reinicio"])

    def test_proyeccion(self):
        semana = 7 * 86400
        reinicio = 1_000_000.0
        inicio = reinicio - semana
        # 50% usado a un cuarto de la semana: llega al 100% a la mitad, antes del reinicio
        self.assertAlmostEqual(monitor.proyeccion_limite(50, reinicio, semana, inicio + semana / 4),
                               inicio + semana / 2)
        # 10% usado a la mitad de la semana: no llega
        self.assertIsNone(monitor.proyeccion_limite(10, reinicio, semana, inicio + semana / 2))

    def test_pagina_con_datos_y_sin_token(self):
        self.escribir_cred(accessToken="token-que-no-debe-aparecer", expiresAt=(time.time() + 3600) * 1000)
        monitor._claude.update(datos={"five_hour": {"utilization": 17, "resets_at": "2099-01-01T00:00:00Z"},
                                      "seven_day": {"utilization": 25, "resets_at": "2099-01-05T08:00:00Z"}},
                               actualizado=time.time())
        _, _, _, secciones = monitor.generar_claude()
        cuerpo = "".join(secciones)
        self.assertIn("17", cuerpo)
        self.assertIn("25", cuerpo)
        self.assertNotIn("token-que-no-debe-aparecer", cuerpo)

    def test_error_se_escapa(self):
        monitor._claude.update(error="<script>x</script>")
        cuerpo = "".join(monitor.generar_claude()[3])
        self.assertNotIn("<script>x", cuerpo)


class TestServidor(unittest.TestCase):
    """Levanta el servidor de verdad en un puerto libre y le hace pedidos HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.servidor = monitor.ThreadingHTTPServer(("127.0.0.1", 0), monitor.Manejador)
        cls.base = f"http://127.0.0.1:{cls.servidor.server_address[1]}"
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()

    def pedir(self, ruta):
        try:
            with urllib.request.urlopen(self.base + ruta, timeout=5) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""

    def test_paginas_responden(self):
        for ruta in ("/", "/clima", "/musica", "/pomodoro", "/notas", "/claude", "/apk"):
            estado, cuerpo = self.pedir(ruta)
            self.assertEqual(estado, 200, ruta)
            self.assertIn("target-densitydpi=device-dpi", cuerpo, ruta)

    def test_parcial_trae_la_pagina_sugerida_al_final(self):
        _, cuerpo = self.pedir(f"/?parcial=1&v={monitor.VERSION}")
        self.assertIn(cuerpo.split(monitor.SEPARADOR)[-1].lstrip("!"),
                      ("/", "/clima", "/musica", "/pomodoro", "/notas"))

    def test_no_se_puede_salir_de_la_carpeta_apk(self):
        self.assertEqual(self.pedir("/apk/..%2Fmonitor.py")[0], 404)
        self.assertEqual(self.pedir("/apk/../monitor.py")[0], 404)

    def test_icono_invalido(self):
        self.assertEqual(self.pedir("/icono?t=../etc&s=10")[0], 404)

    def test_panel_desde_la_pc(self):
        self.assertEqual(self.pedir("/control")[0], 200)
        estado, cuerpo = self.pedir("/control/estado")
        self.assertEqual(estado, 200)
        self.assertIn("pomodoro", json.loads(cuerpo))

    def test_panel_bloqueado_desde_otra_ip(self):
        # Conexion saliendo desde 127.0.0.5: para el servidor es otro dispositivo de la red
        import http.client
        puerto = self.servidor.server_address[1]
        for metodo, ruta in (("GET", "/control"), ("GET", "/control/ir?pagina=clima"),
                             ("GET", "/control/notas/borrar?id=todas"), ("POST", "/control/notas")):
            c = http.client.HTTPConnection("127.0.0.1", puerto, timeout=5,
                                           source_address=("127.0.0.5", 0))
            c.request(metodo, ruta, body=b'{"texto": "x"}' if metodo == "POST" else None)
            self.assertEqual(c.getresponse().status, 403, ruta)
            c.close()


class TestConfig(unittest.TestCase):
    """config.json pisa los valores de fabrica sin tocar el codigo."""

    def setUp(self):
        self.antes = {k: getattr(monitor, k) for k in ("ZONA_NOMBRE", "ZONA_LAT", "POMO_CADA")}
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        for k, v in self.antes.items():
            setattr(monitor, k, v)
        self.dir.cleanup()

    def cargar(self, contenido):
        with open(os.path.join(self.dir.name, "config.json"), "w", encoding="utf-8") as f:
            f.write(contenido)
        falso = os.path.join(self.dir.name, "monitor.py")
        with mock.patch.object(monitor, "__file__", falso), mock.patch("builtins.print"), \
                mock.patch.dict(os.environ, {"MONITOR_SIN_CONFIG": ""}):
            monitor._cargar_config()

    def test_pisa_valores_conocidos(self):
        self.cargar('{"ZONA_NOMBRE": "Mi barrio", "ZONA_LAT": -10.5, "POMO_CADA": 3}')
        self.assertEqual((monitor.ZONA_NOMBRE, monitor.ZONA_LAT, monitor.POMO_CADA),
                         ("Mi barrio", -10.5, 3))

    def test_ignora_claves_desconocidas_o_internas(self):
        self.cargar('{"NO_EXISTE": 1, "_pomo": null, "estado": 2}')
        self.assertFalse(hasattr(monitor, "NO_EXISTE"))
        self.assertIsInstance(monitor._pomo, dict)
        self.assertIsInstance(monitor.estado, dict)

    def test_json_roto_no_rompe_nada(self):
        self.cargar('{"ZONA_NOMBRE": ')
        self.assertEqual(monitor.ZONA_NOMBRE, self.antes["ZONA_NOMBRE"])


if __name__ == "__main__":
    unittest.main()
