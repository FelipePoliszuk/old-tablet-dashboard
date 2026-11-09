#!/usr/bin/env python3
"""
Monitor de la PC para mostrar en una tablet vieja (Android 4.0).

Lee todo directo de /proc y /sys: no usa librerias externas.
La pagina es HTML + CSS simple con tablas, para que la entienda el navegador
de Android 4.0. Se carga una sola vez y despues un JavaScript viejo (ES5) pide
solo los datos y actualiza las partes que cambiaron, sin recargar ni parpadear.

Paginas:
    /         monitor de la PC (CPU, RAM, temperatura, red, disco)
    /musica   lo que esta sonando (Spotify u otro reproductor), con controles.
              Necesita playerctl:  sudo apt install playerctl
    /clima    clima de tu zona (Open-Meteo), con aviso de lluvia y proximas horas
    /pomodoro temporizador pomodoro (25/5, descanso largo cada 4)
    /notas    notas grandes que se escriben desde el panel (se guardan en notas.json)
    /apk      APKs de la carpeta apk/ para instalar en la tablet
    /control  panel para manejar la tablet desde la PC (solo abre desde la PC:
              http://localhost:8080/control)

Rotacion inteligente (se puede apagar: modo fijo, desde el panel o con
ROTACION_AUTOMATICA en config.json): si suena musica muestra /musica; si no suena nada durante
ESPERA_SIN_MUSICA segundos vuelve a /. Si elegis una pagina a mano, se respeta
durante BLOQUEO_MANUAL segundos. /apk nunca cambia sola.

Uso:
    python3 monitor.py
y en la tablet abrir http://IP-DE-TU-PC:8080

Configuracion: los valores de abajo son los de fabrica. Para cambiarlos sin tocar
el codigo, copia config.example.json como config.json y edita lo que quieras
(config.json no se sube a git: ahi van tus datos, como tu barrio).
"""
import glob
import hashlib
import html
import json
import os
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUERTO = 8080
REFRESCO = 3          # segundos entre recargas de la pagina
MUESTREO = 2          # segundos entre mediciones de CPU y red

# Rotacion inteligente entre paginas
ESPERA_SIN_MUSICA = 180   # seg sin que suene nada para volver de Musica al Monitor
BLOQUEO_MANUAL = 300      # seg que se respeta una pagina elegida a mano antes de rotar sola
ROTAR_SIN_MUSICA = ["/", "/clima"]  # paginas que van alternando cuando no suena musica
ROTACION_SEG = 30         # seg que se muestra cada una de esas paginas
ROTACION_AUTOMATICA = True  # False = modo fijo: la tablet no cambia de pagina sola
                            # (se puede prender y apagar desde el panel)

# Clima (Open-Meteo: gratis y sin registro). Tu zona va en config.json.
ZONA_NOMBRE = "Buenos Aires"
ZONA_LAT = -34.6037
ZONA_LON = -58.3816
ZONA_HUSO = "America/Argentina/Buenos_Aires"
CLIMA_CADA = 600          # seg entre consultas del pronostico (10 minutos)
# Se puede cambiar con una variable de entorno (sirve para pruebas)
CLIMA_API = os.environ.get("MONITOR_CLIMA_API", "https://api.open-meteo.com/v1/forecast")

# Pomodoro
POMO_TRABAJO = 25 * 60   # seg de concentracion
POMO_DESCANSO = 5 * 60   # seg de descanso corto
POMO_LARGO = 15 * 60     # seg de descanso largo
POMO_CADA = 4            # cada cuantos pomodoros toca el descanso largo


def _cargar_config():
    """Pisa los valores de arriba con los de config.json (si existe)."""
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(ruta, encoding="utf-8") as f:
            datos = json.load(f)
    except FileNotFoundError:
        return
    except ValueError as e:
        print(f"config.json tiene un error y se ignora: {e}")
        return
    g = globals()
    for clave, valor in datos.items():
        if clave.isupper() and clave in g and not clave.startswith("_"):
            g[clave] = valor
        else:
            print(f"config.json: '{clave}' no es una opcion conocida (se ignora)")


_cargar_config()
PAGINA = os.sysconf("SC_PAGE_SIZE")

# Version de las paginas = huella de este archivo. Si cambias monitor.py y reinicias,
# la tablet detecta que tiene una version vieja y se recarga sola (no hace falta tocarla).
with open(os.path.abspath(__file__), "rb") as _f:
    VERSION = hashlib.sha1(_f.read()).hexdigest()[:8]

# Lo que mide el hilo de fondo (CPU y red necesitan dos lecturas para sacar un %)
estado = {"cpu": 0.0, "nucleos": [], "rx": 0.0, "tx": 0.0}


def leer(ruta):
    with open(ruta) as f:
        return f.read().strip()


# ---------- Lecturas de /proc y /sys ----------

def leer_cpu():
    """Devuelve {cpu: (idle, total)} con los contadores de /proc/stat."""
    tiempos = {}
    with open("/proc/stat") as f:
        for linea in f:
            if not linea.startswith("cpu"):
                break
            partes = linea.split()
            # user nice system idle iowait irq softirq steal
            vals = [int(x) for x in partes[1:9]]
            idle = vals[3] + vals[4]
            tiempos[partes[0]] = (idle, sum(vals))
    return tiempos


def leer_red():
    """Bytes recibidos y enviados por todas las interfaces menos lo."""
    rx = tx = 0
    with open("/proc/net/dev") as f:
        for linea in f.readlines()[2:]:
            nombre, datos = linea.split(":", 1)
            if nombre.strip() == "lo":
                continue
            campos = datos.split()
            rx += int(campos[0])
            tx += int(campos[8])
    return rx, tx


def leer_memoria():
    info = {}
    with open("/proc/meminfo") as f:
        for linea in f:
            clave, valor = linea.split(":", 1)
            info[clave] = int(valor.split()[0]) * 1024  # viene en kB
    total = info["MemTotal"]
    usada = total - info["MemAvailable"]
    swap_usada = info.get("SwapTotal", 0) - info.get("SwapFree", 0)
    return total, usada, info.get("SwapTotal", 0), swap_usada


def leer_temperatura():
    """Temperatura del CPU en grados, o None si no se encuentra."""
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            nombre = leer(d + "/name")
        except OSError:
            continue
        if nombre in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            try:
                return int(leer(d + "/temp1_input")) / 1000
            except (OSError, ValueError):
                pass
    temps = []
    for z in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            temps.append(int(leer(z)) / 1000)
        except (OSError, ValueError):
            pass
    return max(temps) if temps else None


def top_procesos(n=5):
    """Procesos que mas RAM usan, agrupados por nombre (ej: todos los 'chrome')."""
    por_nombre = {}
    for p in glob.glob("/proc/[0-9]*"):
        try:
            nombre = leer(p + "/comm")
            rss = int(leer(p + "/statm").split()[1]) * PAGINA
        except (OSError, ValueError, IndexError):
            continue  # el proceso termino mientras lo leiamos
        cant, total = por_nombre.get(nombre, (0, 0))
        por_nombre[nombre] = (cant + 1, total + rss)
    orden = sorted(por_nombre.items(), key=lambda x: x[1][1], reverse=True)
    return orden[:n]


def muestreador():
    """Hilo de fondo: compara dos lecturas para calcular % de CPU y velocidad de red."""
    cpu_ant, red_ant, t_ant = leer_cpu(), leer_red(), time.time()
    while True:
        time.sleep(MUESTREO)
        cpu, red, t = leer_cpu(), leer_red(), time.time()

        def uso(clave):
            d_idle = cpu[clave][0] - cpu_ant[clave][0]
            d_total = cpu[clave][1] - cpu_ant[clave][1]
            return 100.0 * (1 - d_idle / d_total) if d_total else 0.0

        nucleos = sorted((k for k in cpu if k != "cpu"), key=lambda k: int(k[3:]))
        estado["cpu"] = uso("cpu")
        estado["nucleos"] = [uso(k) for k in nucleos]
        dt = t - t_ant
        estado["rx"] = (red[0] - red_ant[0]) / dt
        estado["tx"] = (red[1] - red_ant[1]) / dt
        cpu_ant, red_ant, t_ant = cpu, red, t


# ---------- Formato ----------

def fmt_bytes(n):
    for unidad in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unidad}" if unidad == "B" else f"{n:.1f} {unidad}"
        n /= 1024
    return f"{n:.1f} PB"


DIAS = ["lunes", "martes", "mi&eacute;rcoles", "jueves", "viernes", "s&aacute;bado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def fecha_es():
    t = time.localtime()
    return f"{DIAS[t.tm_wday]} {t.tm_mday} de {MESES[t.tm_mon - 1]}"


def fmt_uptime(seg):
    d, resto = divmod(int(seg), 86400)
    h, resto = divmod(resto, 3600)
    m = resto // 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


def color(pct):
    if pct < 60:
        return "#4caf50"
    if pct < 85:
        return "#ffb300"
    return "#e53935"


def barra(pct, alto=18):
    pct = max(0, min(100, pct))
    return (f'<div class="barra" style="height:{alto}px">'
            f'<div style="width:{pct:.0f}%;height:{alto}px;background:{color(pct)}"></div></div>')


def barras_nucleos(valores):
    """Barras verticales chiquitas, una por nucleo (tabla: anda en navegadores viejos)."""
    alto = 46
    celdas = "".join(
        f'<td class="nucleo"><div style="height:{max(1, v * alto / 100):.0f}px;'
        f'background:{color(v)}"></div></td>'
        for v in valores
    )
    return f'<table class="nucleos" style="height:{alto}px"><tr>{celdas}</tr></table>'


def generar_html():
    total, usada, swap_total, swap_usada = leer_memoria()
    ram_pct = 100 * usada / total
    temp = leer_temperatura()
    carga = leer("/proc/loadavg").split()[:3]
    uptime = float(leer("/proc/uptime").split()[0])
    st = os.statvfs("/")
    disco_total = st.f_blocks * st.f_frsize
    disco_usado = disco_total - st.f_bavail * st.f_frsize
    disco_pct = 100 * disco_usado / disco_total
    cpu = estado["cpu"]

    temp_txt = f"{temp:.0f}&deg;C" if temp is not None else "s/d"
    temp_color = "#eee" if temp is None else color((temp - 30) * 100 / 60)  # 30..90 grados

    filas_proc = "".join(
        f"<tr><td>{html.escape(nombre)}"
        f"{f' <span class=tenue>x{cant}</span>' if cant > 1 else ''}</td>"
        f'<td class="der">{fmt_bytes(rss)}</td></tr>'
        for nombre, (cant, rss) in top_procesos()
    )

    swap_txt = (f'<div class="tenue chico">swap {fmt_bytes(swap_usada)} / {fmt_bytes(swap_total)}</div>'
                if swap_total else "")

    cabecera = f"""<table class="cab"><tr>
  <td class="reloj">{time.strftime("%H:%M")}</td>
  <td class="fecha">{fecha_es()}<br>
    <span class="tenue">{html.escape(socket.gethostname())} &middot; encendida hace {fmt_uptime(uptime)}</span></td>
  <td class="der" style="vertical-align:top">{nav("monitor")}</td>
</tr></table>"""

    cuerpo = f"""<table class="grilla"><tr>
  <td class="caja fila1" style="width:50%">
    <div class="titulo">CPU</div>
    <div class="grande" style="color:{color(cpu)}">{cpu:.0f}<span class="unidad">%</span></div>
    {barras_nucleos(estado["nucleos"])}
    <div class="tenue chico">carga {" &middot; ".join(carga)}</div>
  </td>
  <td class="caja fila1" style="width:22%">
    <div class="titulo">Temperatura</div>
    <div class="grande" style="color:{temp_color}">{temp_txt}</div>
  </td>
  <td class="caja fila1" style="width:28%">
    <div class="titulo">Red</div>
    <div class="medio" style="margin-top:10px"><span class="flecha">&darr;</span> {fmt_bytes(estado["rx"])}/s</div>
    <div class="medio" style="margin-top:6px"><span class="flecha">&uarr;</span> {fmt_bytes(estado["tx"])}/s</div>
  </td>
</tr></table>

<table class="grilla"><tr>
  <td class="caja fila2" style="width:50%">
    <div class="titulo">RAM <span class="pct">{ram_pct:.0f}%</span></div>
    <div class="medio">{fmt_bytes(usada)} <span class="tenue">/ {fmt_bytes(total)}</span></div>
    {barra(ram_pct, 10)}
    {swap_txt}
    <div class="titulo" style="margin-top:12px">Disco <span class="pct">{disco_pct:.0f}%</span></div>
    <div class="medio">{fmt_bytes(disco_usado)} <span class="tenue">/ {fmt_bytes(disco_total)}</span></div>
    {barra(disco_pct, 10)}
  </td>
  <td class="caja fila2" style="width:50%">
    <div class="titulo">Lo que m&aacute;s RAM usa</div>
    <table class="procs">{filas_proc}</table>
  </td>
</tr></table>"""

    return "Monitor", CSS_MONITOR, "{s0}\n{s1}", [cabecera, cuerpo]


CSS_MONITOR = """
.cab { width:100%; margin-bottom:2px; }
.reloj { font-size:56px; font-weight:bold; line-height:56px; width:1%; padding-right:16px;
         letter-spacing:-1px; }
.fecha { font-size:17px; color:#e6e6e6; line-height:24px; vertical-align:middle; }
.grilla { width:100%; }
.caja { background:#1b1d22; padding:10px 12px; vertical-align:top; border:4px solid #0e0f12; }
.fila1 { height:118px; }
.fila2 { height:150px; }
.titulo { font-size:13px; color:#9aa0aa; letter-spacing:1px; text-transform:uppercase; }
.pct { color:#e6e6e6; float:right; letter-spacing:0; }
.grande { font-size:48px; font-weight:bold; line-height:54px; }
.unidad { font-size:26px; }
.medio { font-size:21px; font-weight:bold; margin-top:2px; }
.flecha { color:#4fc3f7; }
.chico { font-size:13px; margin-top:4px; }
.barra { background:#2c3038; width:100%; margin:6px 0 2px 0; }
.nucleos { width:100%; margin-top:4px; }
.nucleo { vertical-align:bottom; padding:0 1px; background:#262a31; border-left:2px solid #1b1d22; }
.procs { width:100%; font-size:16px; margin-top:6px; }
.procs td { padding:2px 0; border-bottom:1px solid #262a31; }
"""


# ---------- Plantilla comun: carga una vez y despues actualiza por partes ----------

SEPARADOR = "\x1e"  # caracter "separador de registros": nunca aparece en el HTML

# JavaScript ES5 a proposito: el navegador de Android 4.0 no entiende let, =>, fetch, etc.
# Cada N segundos pide solo los datos (?parcial=1) y reemplaza unicamente las secciones
# que cambiaron, asi la pagina no parpadea y la portada no se redibuja si es la misma.
SCRIPT = """
<script>
var ultimo = __INICIAL__;
var ocupado = false;
// Si la pagina se eligio a mano (?manual=1), no rotar sola hasta que pase un rato
var manualHasta = (location.search.indexOf('manual=1') >= 0)
    ? new Date().getTime() + __MANUAL_MS__ : 0;
function actualizar() {
  if (ocupado) { return; }
  ocupado = true;
  var x = new XMLHttpRequest();
  x.open('GET', location.pathname + '?parcial=1&v=__VERSION__&t=' + new Date().getTime(), true);
  x.onreadystatechange = function () {
    if (x.readyState != 4) { return; }
    ocupado = false;
    if (x.status != 200) { return; }
    var partes = x.responseText.split('\\u001e');
    // La ultima parte es la pagina a mostrar:
    //  "!/musica?manual=1" = orden del panel de la PC: se obedece siempre
    //  "/musica"           = sugerencia de la rotacion: se respeta la eleccion manual y Apps
    var destino = partes.pop();
    if (destino && destino.charAt(0) == '!') {
      location.href = destino.substring(1);
      return;
    }
    if (destino && destino != location.pathname && location.pathname != '/apk'
        && location.search.indexOf('vista=1') < 0
        && new Date().getTime() > manualHasta) {
      location.href = destino;
      return;
    }
    for (var i = 0; i < partes.length; i++) {
      if (partes[i] === ultimo[i]) { continue; }
      var el = document.getElementById('s' + i);
      if (el) { el.innerHTML = partes[i]; }
      ultimo[i] = partes[i];
    }
  };
  x.send(null);
}
function ctl(accion) {
  var x = new XMLHttpRequest();
  x.open('GET', '/control?accion=' + accion + '&ajax=1', true);
  x.onreadystatechange = function () {
    if (x.readyState == 4) { ocupado = false; actualizar(); }
  };
  x.send(null);
  return false;
}
// Accion en el servidor (pomodoro, notas) sin recargar; el href queda de respaldo
function ir(url) {
  var x = new XMLHttpRequest();
  x.open('GET', url + (url.indexOf('?') >= 0 ? '&' : '?') + 'ajax=1', true);
  x.onreadystatechange = function () {
    if (x.readyState == 4) { ocupado = false; actualizar(); }
  };
  x.send(null);
  return false;
}
// Cuenta regresiva del pomodoro: avanza cada segundo en la tablet (el servidor
// manda los segundos que faltan; asi no importa si el reloj de la tablet esta mal)
function tick() {
  var c = document.getElementById('cuenta');
  if (!c || c.getAttribute('data-corriendo') != '1') { return; }
  var ahora = new Date().getTime();
  if (!c.fin) { c.fin = ahora + parseInt(c.getAttribute('data-restante'), 10) * 1000; }
  var r = Math.max(0, Math.ceil((c.fin - ahora) / 1000));
  var m = Math.floor(r / 60), sg = r % 60;
  c.innerHTML = (m < 10 ? '0' : '') + m + ':' + (sg < 10 ? '0' : '') + sg;
}
setInterval(tick, 500);
setInterval(actualizar, __MS__);
setTimeout(function () { location.reload(); }, 3600000);
</script>
"""


def pagina(titulo, css, plantilla, secciones):
    cuerpo = plantilla
    for i, sec in enumerate(secciones):
        cuerpo = cuerpo.replace("{s%d}" % i, f'<div id="s{i}">{sec}</div>')
    # El script arranca sabiendo que hay en pantalla, asi la primera actualizacion
    # no redibuja todo (por ejemplo la portada). "<\/" evita cerrar el <script> antes.
    inicial = json.dumps(secciones).replace("</", "<\\/")
    script = (SCRIPT.replace("__MS__", str(REFRESCO * 1000))
              .replace("__MANUAL_MS__", str(BLOQUEO_MANUAL * 1000))
              .replace("__VERSION__", VERSION)
              .replace("__INICIAL__", inicial))
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<!-- target-densitydpi=device-dpi: la tablet se reporta como "baja densidad" y el navegador
     de Android 4.0 achicaba todo al 75%. Con esto, 1 px de CSS = 1 pixel real (800x480). -->
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, target-densitydpi=device-dpi">
<title>{titulo}</title>
<style>
body {{ background:#0e0f12; color:#f2f2f2; font-family:sans-serif; margin:0; padding:10px; }}
table {{ border-collapse:collapse; }}
td {{ padding:0; }}
.der {{ text-align:right; }}
.tenue {{ color:#9aa0aa; }}
{CSS_NAV}
{css}
</style>
</head><body>
{cuerpo}
{script}
</body></html>"""


# ---------- Navegacion entre paginas ----------

CSS_NAV = """
.nav { white-space:nowrap; }
.nav a { color:#c8ccd4; text-decoration:none; font-size:14px; padding:6px 9px;
         margin-left:3px; background:#262a31; }
.nav a.activa { color:#0e0f12; background:#1db954; font-weight:bold; }
"""


def nav(activa):
    links = [("monitor", "/", "Monitor"), ("clima", "/clima", "Clima"),
             ("musica", "/musica", "M&uacute;sica"), ("pomodoro", "/pomodoro", "Pomodoro"),
             ("notas", "/notas", "Notas"), ("apk", "/apk", "Apps")]
    # ?manual=1 le avisa a la pagina que la eligio una persona: no rotar por un rato
    return '<span class="nav">' + "".join(
        f'<a href="{url}?manual=1"{" class=activa" if clave == activa else ""}>{texto}</a>'
        for clave, url, texto in links
    ) + "</span>"


# ---------- Musica (playerctl / MPRIS) ----------

# Prioridad: Spotify; si no esta abierto, cualquier otro reproductor (Firefox, VLC, etc.)
JUGADORES = "spotify,%any"
ACCIONES = {"play-pause", "next", "previous"}
_portadas = {}  # clave -> (bytes, content-type); cache chica de portadas


def leer_musica():
    """Devuelve un dict con lo que esta sonando, o un str explicando por que no hay nada."""
    formato = "\t".join([
        "{{status}}", "{{artist}}", "{{title}}", "{{album}}",
        "{{mpris:artUrl}}", "{{mpris:length}}", "{{position}}", "{{playerName}}",
    ])
    try:
        r = subprocess.run(
            ["playerctl", "--player", JUGADORES, "metadata", "--format", formato],
            capture_output=True, text=True, timeout=2,
        )
    except FileNotFoundError:
        return "Falta instalar playerctl:  sudo apt install playerctl"
    except subprocess.TimeoutExpired:
        return "playerctl no respondi&oacute;"
    if r.returncode != 0 or not r.stdout.strip():
        return "No est&aacute; sonando nada"

    campos = (r.stdout.rstrip("\n").split("\t") + [""] * 8)[:8]
    estado_, artista, titulo, album, arte, largo, pos, jugador = campos

    def a_seg(us):
        try:
            return int(us) / 1_000_000
        except ValueError:
            return 0.0

    return {
        "estado": estado_, "artista": artista, "titulo": titulo, "album": album,
        "arte": arte, "largo": a_seg(largo), "pos": a_seg(pos), "jugador": jugador,
    }


def clave_portada(url):
    return hashlib.sha1(url.encode()).hexdigest()[:16]


TAM_PORTADA = 392  # px: alto util de la pantalla (480) menos la barra de Android y margenes

try:
    from PIL import Image  # opcional: sudo apt install python3-pil
except ImportError:
    Image = None


def bajar_portada(url):
    """La tablet no puede abrir HTTPS, asi que la PC baja la portada y se la pasa por HTTP.

    Si esta Pillow, ademas la achica al tamano exacto de la pantalla (menos trabajo para
    la tablet) y saca el color promedio para tenir el fondo. Devuelve (bytes, tipo, color).
    """
    clave = clave_portada(url)
    if clave in _portadas:
        return _portadas[clave]
    if url.startswith("file://"):
        ruta = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        with open(ruta, "rb") as f:
            datos = f.read()
        tipo = "image/png" if ruta.lower().endswith(".png") else "image/jpeg"
    else:
        with urllib.request.urlopen(url, timeout=5) as resp:
            datos = resp.read()
            tipo = resp.headers.get("Content-Type", "image/jpeg")

    color_ = None
    if Image is not None:
        try:
            import io
            img = Image.open(io.BytesIO(datos)).convert("RGB")
            color_ = img.resize((1, 1), Image.BILINEAR).getpixel((0, 0))
            img = img.resize((TAM_PORTADA, TAM_PORTADA), Image.LANCZOS)
            salida = io.BytesIO()
            img.save(salida, "JPEG", quality=88)
            datos, tipo = salida.getvalue(), "image/jpeg"
        except Exception:
            pass  # si falla, se usa la imagen original sin color

    if len(_portadas) > 20:
        _portadas.clear()
    _portadas[clave] = (datos, tipo, color_)
    return _portadas[clave]


def estilo_fondo(rgb):
    """Fondo con un degrade suave del color de la portada hacia el negro.
    Se dibuja una sola vez por cancion, asi que no le cuesta nada a la tablet."""
    if not rgb:
        return "<style>body { background:#0e0f12; }</style>"
    r, g, b = (int(c * 0.38) for c in rgb)  # oscurecido para que el texto se lea
    return (f"<style>body {{ background-color:#0e0f12; "
            f"background-image:-webkit-linear-gradient(left, rgb({r},{g},{b}) 0%, #0e0f12 85%); "
            f"background-image:linear-gradient(to right, rgb({r},{g},{b}) 0%, #0e0f12 85%); }}</style>")


def fmt_tiempo(seg):
    m, s = divmod(int(seg), 60)
    return f"{m}:{s:02d}"


def generar_musica():
    m = leer_musica()

    # Los botones van abajo a la derecha (posicion absoluta) para que no compitan
    # en ancho con la hora: antes empujaban la columna fuera de la pantalla.
    cabecera = f"""<div class="nav-esquina">{nav("musica")}</div>
<div class="reloj">{time.strftime("%H:%M")}</div><div class="fecha">{fecha_es()}</div>"""

    sin_portada = f'<div class="sinportada" style="width:{TAM_PORTADA}px;height:{TAM_PORTADA}px;line-height:{TAM_PORTADA}px">&#9835;</div>'

    if isinstance(m, str):
        portada = sin_portada
        fondo = estilo_fondo(None)
        info = f'<div class="vacio">{m}</div>'
    else:
        # Guardamos la URL de la portada para poder servirla en /portada?k=...
        color_ = None
        if m["arte"]:
            clave = clave_portada(m["arte"])
            _urls_portada[clave] = m["arte"]
            try:
                color_ = bajar_portada(m["arte"])[2]
            except Exception:
                pass
            portada = (f'<img src="/portada?k={clave}" width="{TAM_PORTADA}" '
                       f'height="{TAM_PORTADA}" class="portada">')
        else:
            portada = sin_portada
        fondo = estilo_fondo(color_)

        pct = 100 * m["pos"] / m["largo"] if m["largo"] else 0
        sonando = m["estado"] == "Playing"
        boton_pp = "Pausa" if sonando else "Play"
        estado_txt = "" if sonando else ' &middot; <span class="pausado">EN PAUSA</span>'

        def boton(accion, texto, clase=""):
            # onclick: manda la orden sin recargar; href: respaldo si falla el JavaScript
            return (f'<a href="/control?accion={accion}" onclick="return ctl(\'{accion}\')"'
                    f'{" class=" + clase if clase else ""}>{texto}</a>')

        info = f"""<div class="jugador">{html.escape(m["jugador"].upper())}{estado_txt}</div>
    <div class="titulo-cancion">{html.escape(m["titulo"]) or "Sin t&iacute;tulo"}</div>
    <div class="artista">{html.escape(m["artista"])}</div>
    <div class="album">{html.escape(m["album"])}</div>
    <div class="progreso"><div style="width:{pct:.1f}%"></div></div>
    <table style="width:100%" class="tiempos"><tr>
      <td>{fmt_tiempo(m["pos"])}</td><td class="der">{fmt_tiempo(m["largo"])}</td>
    </tr></table>
    <table class="controles"><tr>
      <td>{boton("previous", "&laquo;", "flecha")}</td>
      <td style="width:50%">{boton("play-pause", boton_pp, "principal")}</td>
      <td>{boton("next", "&raquo;", "flecha")}</td>
    </tr></table>"""

    # table-layout:fixed = la columna derecha mide exactamente lo que sobra y su
    # contenido ya no puede agrandarla (lo que no entra se corta adentro, no en la pantalla)
    plantilla = f"""{{s3}}
<table class="lienzo"><tr>
  <td style="width:{TAM_PORTADA}px;vertical-align:top">{{s1}}</td>
  <td class="col-info">{{s0}}{{s2}}</td>
</tr></table>"""
    return "M&uacute;sica", CSS_MUSICA, plantilla, [cabecera, portada, info, fondo]


CSS_MUSICA = """
.portada { display:block; }
.lienzo { width:100%; table-layout:fixed; }
.col-info { vertical-align:top; padding-left:22px; overflow:hidden; }
.nav-esquina { position:absolute; bottom:8px; right:10px; width:240px; text-align:right; }
.nav-esquina .nav { white-space:normal; }
.nav-esquina a { display:inline-block; margin-top:4px; }
.tiempos { table-layout:fixed; }
.reloj { font-size:64px; font-weight:bold; line-height:62px; letter-spacing:-1px; }
.fecha { font-size:16px; color:#c8ccd4; margin-top:2px; }
.jugador { font-size:12px; color:#9aa0aa; letter-spacing:2px; margin-top:26px; }
.pausado { color:#ffb300; }
.titulo-cancion { font-size:27px; font-weight:bold; margin-top:6px; line-height:31px;
                  max-height:62px; overflow:hidden; }
.artista { font-size:20px; color:#1ed760; margin-top:6px; white-space:nowrap; overflow:hidden; }
.album { font-size:15px; color:#9aa0aa; margin-top:3px; white-space:nowrap; overflow:hidden; }
.progreso { background:rgba(255,255,255,0.18); height:6px; margin-top:18px; }
.progreso div { background:#f2f2f2; height:6px; }
.tiempos { font-size:13px; color:#c8ccd4; margin-top:5px; }
.controles { width:100%; margin-top:16px; }
.controles td { text-align:center; padding:0 3px; }
.controles a { display:block; background:rgba(255,255,255,0.12); color:#f2f2f2;
               text-decoration:none; font-size:18px; height:52px; line-height:52px; }
.controles a.flecha { font-size:30px; }
.controles a.principal { background:#1db954; color:#0e0f12; font-weight:bold; font-size:20px; }
.sinportada { background:#1b1d22; color:#3a3f48; font-size:150px; text-align:center; }
.vacio { font-size:22px; color:#c8ccd4; margin-top:60px; }
"""


_urls_portada = {}  # clave -> URL original de la portada


# ---------- Rotacion inteligente ----------

_ultimo_sonando = 0.0  # momento (time.time) en que se vio sonar musica por ultima vez


def vigilar_musica():
    """Hilo de fondo: anota cuando suena musica, este la tablet en la pagina que este."""
    global _ultimo_sonando
    while True:
        m = leer_musica()
        if isinstance(m, dict) and m["estado"] == "Playing":
            _ultimo_sonando = time.time()
        time.sleep(MUESTREO)


_rotacion = {"activa": ROTACION_AUTOMATICA}


def pagina_sugerida():
    """Prioridades: 1) pomodoro corriendo, 2) musica sonando (o sono hace poco),
    3) alternar entre ROTAR_SIN_MUSICA (+ Notas si hay) cada ROTACION_SEG segundos.
    En modo fijo devuelve "": la tablet se queda en la pagina que tiene."""
    if not _rotacion["activa"]:
        return ""
    if _pomo["corriendo"]:
        return "/pomodoro"
    if time.time() - _ultimo_sonando < ESPERA_SIN_MUSICA:
        return "/musica"
    lista = list(ROTAR_SIN_MUSICA)
    if _notas and "/notas" not in lista:
        lista.append("/notas")
    turno = int(time.time() // ROTACION_SEG) % len(lista)
    return lista[turno]


# ---------- Clima ----------

_clima = {"datos": None, "actualizado": 0.0, "error": None}

DESCRIPCIONES = {
    0: "Despejado", 1: "Mayormente despejado", 2: "Parcialmente nublado", 3: "Nublado",
    45: "Niebla", 48: "Niebla", 51: "Llovizna d&eacute;bil", 53: "Llovizna", 55: "Llovizna intensa",
    56: "Llovizna helada", 57: "Llovizna helada", 61: "Lluvia d&eacute;bil", 63: "Lluvia",
    65: "Lluvia fuerte", 66: "Lluvia helada", 67: "Lluvia helada", 71: "Nieve d&eacute;bil",
    73: "Nieve", 75: "Nieve fuerte", 77: "Granizo fino", 80: "Chaparrones", 81: "Chaparrones",
    82: "Chaparrones fuertes", 85: "Nevadas", 86: "Nevadas fuertes", 95: "Tormenta",
    96: "Tormenta con granizo", 99: "Tormenta con granizo",
}


def tipo_icono(codigo, es_dia=True):
    """Codigo del clima (WMO) -> nombre del icono a dibujar."""
    if codigo in (0, 1):
        return "sol" if es_dia else "luna"
    if codigo == 2:
        return "solnube" if es_dia else "lunanube"
    if codigo == 3:
        return "nube"
    if codigo in (45, 48):
        return "niebla"
    if codigo >= 95:
        return "tormenta"
    if codigo in (71, 73, 75, 77, 85, 86):
        return "nieve"
    return "lluvia"


def bajar_clima():
    params = urllib.parse.urlencode({
        "latitude": ZONA_LAT, "longitude": ZONA_LON, "timezone": ZONA_HUSO, "forecast_days": 3,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,is_day,"
                   "weather_code,wind_speed_10m",
        "hourly": "temperature_2m,precipitation_probability,weather_code,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    })
    with urllib.request.urlopen(f"{CLIMA_API}?{params}", timeout=10) as r:
        return json.loads(r.read().decode())


def vigilar_clima():
    """Hilo de fondo: pide el pronostico cada CLIMA_CADA segundos (si falla, reintenta en 1 min)."""
    while True:
        try:
            _clima.update(datos=bajar_clima(), actualizado=time.time(), error=None)
            time.sleep(CLIMA_CADA)
        except Exception as e:
            _clima["error"] = str(e)
            log(f"[clima] no se pudo actualizar: {e}")
            time.sleep(60)


# Iconos dibujados con Pillow (PNG con transparencia): no dependen de las fuentes
# de la tablet, que no tiene emojis.
_iconos = {}


def dibujar_icono(tipo, tam):
    from PIL import ImageDraw
    import io
    S = tam * 3  # se dibuja grande y se achica: bordes suaves
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    AMARILLO, LUNA = (255, 200, 61, 255), (236, 230, 200, 255)
    NUBE, NUBE_ATRAS, NUBE_OSCURA = (226, 231, 239, 255), (150, 160, 176, 255), (120, 128, 142, 255)
    AZUL, VACIO = (79, 195, 247, 255), (0, 0, 0, 0)

    def circulo(cx, cy, r, color):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)

    def sol(cx, cy, r):
        import math
        for i in range(8):
            a = i * math.pi / 4
            d.line([cx + math.cos(a) * r * 1.35, cy + math.sin(a) * r * 1.35,
                    cx + math.cos(a) * r * 1.75, cy + math.sin(a) * r * 1.75],
                   fill=AMARILLO, width=max(2, int(r * 0.22)))
        circulo(cx, cy, r, AMARILLO)

    def luna(cx, cy, r):
        circulo(cx, cy, r, LUNA)
        circulo(cx + r * 0.55, cy - r * 0.35, r * 0.85, VACIO)

    def nube(cx, cy, w, color):
        h = w * 0.6
        circulo(cx + w * 0.05, cy - h * 0.08, h * 0.42, color)
        circulo(cx - w * 0.24, cy + h * 0.1, h * 0.3, color)
        circulo(cx + w * 0.3, cy + h * 0.12, h * 0.28, color)
        d.rounded_rectangle([cx - w * 0.24, cy + h * 0.02, cx + w * 0.3, cy + h * 0.4],
                            radius=h * 0.18, fill=color)
        d.rectangle([cx - w * 0.24, cy + h * 0.1, cx + w * 0.3, cy + h * 0.4], fill=color)
        circulo(cx - w * 0.24, cy + h * 0.1, h * 0.3, color)
        circulo(cx + w * 0.3, cy + h * 0.12, h * 0.28, color)

    if tipo == "sol":
        sol(S * 0.5, S * 0.5, S * 0.2)
    elif tipo == "luna":
        luna(S * 0.5, S * 0.5, S * 0.3)
    elif tipo in ("solnube", "lunanube"):
        if tipo == "solnube":
            sol(S * 0.38, S * 0.36, S * 0.15)
        else:
            luna(S * 0.38, S * 0.36, S * 0.2)
        nube(S * 0.56, S * 0.58, S * 0.66, NUBE)
    elif tipo == "nube":
        nube(S * 0.4, S * 0.42, S * 0.5, NUBE_ATRAS)
        nube(S * 0.55, S * 0.58, S * 0.7, NUBE)
    elif tipo in ("lluvia", "tormenta", "nieve"):
        nube(S * 0.5, S * 0.4, S * 0.76, NUBE_OSCURA if tipo == "tormenta" else NUBE)
        if tipo == "lluvia":
            for x in (0.32, 0.5, 0.68):
                d.line([S * x, S * 0.68, S * (x - 0.06), S * 0.86], fill=AZUL, width=max(3, S // 28))
        elif tipo == "nieve":
            for x, y in ((0.32, 0.72), (0.5, 0.82), (0.68, 0.72)):
                circulo(S * x, S * y, S * 0.045, (255, 255, 255, 255))
        else:
            d.polygon([(S * .52, S * .6), (S * .4, S * .8), (S * .5, S * .8), (S * .44, S * .96),
                       (S * .64, S * .72), (S * .54, S * .72), (S * .6, S * .6)], fill=AMARILLO)
    else:  # niebla
        for i, y in enumerate((0.36, 0.5, 0.64)):
            x0 = 0.2 if i % 2 == 0 else 0.28
            d.rounded_rectangle([S * x0, S * y - S * 0.035, S * (x0 + 0.56), S * y + S * 0.035],
                                radius=S * 0.035, fill=NUBE_ATRAS)

    img = img.resize((tam, tam), Image.LANCZOS)
    salida = io.BytesIO()
    img.save(salida, "PNG")
    return salida.getvalue()


def icono(tipo, tam):
    """HTML de un icono (o nada, si no esta Pillow)."""
    if Image is None:
        return ""
    return f'<img src="/icono?t={tipo}&s={tam}" width="{tam}" height="{tam}" class="icono">'


def generar_clima():
    cabecera = f"""<table class="cab"><tr>
  <td class="reloj">{time.strftime("%H:%M")}</td>
  <td class="fecha">{fecha_es()}<br><span class="tenue">{html.escape(ZONA_NOMBRE)}</span></td>
  <td class="der" style="vertical-align:top">{nav("clima")}</td>
</tr></table>"""

    datos = _clima["datos"]
    if not datos:
        msg = ("No pude consultar el pron&oacute;stico: " + html.escape(_clima["error"])
               if _clima["error"] else "Buscando el pron&oacute;stico&hellip;")
        return "Clima", CSS_CLIMA, "{s0}{s1}", [cabecera, f'<div class="vacio">{msg}</div>']

    ahora = datos["current"]
    cod = ahora["weather_code"]
    es_dia = bool(ahora.get("is_day", 1))

    # Proximas horas: desde la hora actual en adelante
    h = datos["hourly"]
    hora_actual = time.strftime("%Y-%m-%dT%H:00")
    inicio = next((i for i, t in enumerate(h["time"]) if t >= hora_actual), 0)
    proximas = range(inicio + 1, min(inicio + 15, len(h["time"])), 2)  # cada 2 h, 7 casilleros
    celdas = "".join(
        f'<td class="hora"><div class="h">{h["time"][i][11:13]} h</div>'
        f'{icono(tipo_icono(h["weather_code"][i], bool(h["is_day"][i])), 44)}'
        f'<div class="t">{h["temperature_2m"][i]:.0f}&deg;</div>'
        f'<div class="p{" mojado" if (h["precipitation_probability"][i] or 0) >= 40 else ""}">'
        f'{h["precipitation_probability"][i] or 0}%</div></td>'
        for i in proximas
    )

    # ¿Llueve? Mira las proximas 12 horas
    doce = [(h["time"][i][11:13], h["precipitation_probability"][i] or 0)
            for i in range(inicio, min(inicio + 12, len(h["time"])))]
    maxima = max((p for _, p in doce), default=0)
    if tipo_icono(cod) in ("lluvia", "tormenta"):
        lluvia, clase = "Est&aacute; lloviendo ahora", "llueve"
    elif maxima >= 40:
        hora, p = next((hh, p) for hh, p in doce if p >= 40)
        lluvia, clase = f"Probable lluvia desde las {int(hora)} h ({p}%)", "llueve"
    elif maxima >= 20:
        hora, p = max(doce, key=lambda x: x[1])
        lluvia, clase = f"Chance baja de lluvia ({p}% a las {int(hora)} h)", "quizas"
    else:
        lluvia, clase = "Sin lluvia en las pr&oacute;ximas 12 h", "seco"

    dd = datos["daily"]
    nombres = ["Hoy", "Ma&ntilde;ana"] + [
        DIAS[time.strptime(t, "%Y-%m-%d").tm_wday].capitalize() for t in dd["time"][2:]]
    dias = "".join(
        f'<tr><td class="dnom">{nombres[i]}</td>'
        f'<td>{icono(tipo_icono(dd["weather_code"][i]), 30)}</td>'
        f'<td class="dtemp"><span class="tenue">{dd["temperature_2m_min"][i]:.0f}&deg;</span>'
        f' / {dd["temperature_2m_max"][i]:.0f}&deg;</td>'
        f'<td class="dlluvia">{dd["precipitation_probability_max"][i] or 0}%</td></tr>'
        for i in range(min(3, len(dd["time"])))
    )

    hace = int((time.time() - _clima["actualizado"]) / 60)
    cuerpo = f"""<table class="grilla"><tr>
  <td class="caja actual" style="width:52%">
    <table><tr>
      <td style="width:1%;padding-right:10px">{icono(tipo_icono(cod, es_dia), 104)}</td>
      <td><div class="temp">{ahora["temperature_2m"]:.0f}&deg;</div>
          <div class="desc">{DESCRIPCIONES.get(cod, "")}</div></td>
    </tr></table>
    <div class="detalles">Sensaci&oacute;n {ahora["apparent_temperature"]:.0f}&deg; &middot;
      Humedad {ahora["relative_humidity_2m"]}% &middot; Viento {ahora["wind_speed_10m"]:.0f} km/h</div>
  </td>
  <td class="caja" style="width:48%">
    <div class="aviso {clase}">{lluvia}</div>
    <table class="dias">{dias}</table>
  </td>
</tr></table>
<table class="grilla"><tr><td class="caja">
  <table class="horas"><tr>{celdas}</tr></table>
  <div class="tenue chico">Open-Meteo &middot; actualizado hace {hace} min</div>
</td></tr></table>"""
    return "Clima", CSS_CLIMA, "{s0}{s1}", [cabecera, cuerpo]


CSS_CLIMA = """
.cab { width:100%; margin-bottom:2px; }
.reloj { font-size:56px; font-weight:bold; line-height:56px; width:1%; padding-right:16px; letter-spacing:-1px; }
.fecha { font-size:17px; color:#e6e6e6; line-height:24px; vertical-align:middle; }
.grilla { width:100%; table-layout:fixed; }
.caja { background:#1b1d22; padding:10px 14px; vertical-align:top; border:4px solid #0e0f12; }
.actual { height:150px; }
.icono { display:block; }
.temp { font-size:76px; font-weight:bold; line-height:76px; letter-spacing:-2px; }
.desc { font-size:19px; color:#e6e6e6; margin-top:2px; }
.detalles { font-size:14px; color:#9aa0aa; margin-top:12px; }
.aviso { font-size:18px; font-weight:bold; padding:9px 12px; margin-bottom:8px; }
.aviso.seco { background:#12351f; color:#5ee08a; }
.aviso.quizas { background:#3a3115; color:#ffd54f; }
.aviso.llueve { background:#12304a; color:#7fd3ff; }
.dias { width:100%; font-size:17px; }
.dias td { padding:2px 0; vertical-align:middle; }
.dnom { width:34%; }
.dtemp { text-align:right; font-weight:bold; }
.dlluvia { text-align:right; color:#7fd3ff; width:18%; font-size:15px; }
.horas { width:100%; table-layout:fixed; }
.hora { text-align:center; }
.hora .icono { margin:2px auto; }
.hora .h { font-size:14px; color:#9aa0aa; }
.hora .t { font-size:20px; font-weight:bold; }
.hora .p { font-size:13px; color:#9aa0aa; }
.hora .p.mojado { color:#7fd3ff; font-weight:bold; }
.chico { font-size:12px; margin-top:6px; text-align:right; }
.vacio { font-size:22px; color:#c8ccd4; margin-top:120px; text-align:center; }
"""


# ---------- Panel de control (desde la PC) ----------

_tablet = {"ip": None, "pagina": None, "visto": 0.0}   # ultima vez que la tablet pidio algo
_orden = {"destino": None, "cuando": 0.0}               # orden del panel, se usa una sola vez
_candado = threading.Lock()


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def dar_orden(destino):
    with _candado:
        _orden.update(destino=destino, cuando=time.time())
    log(f"[panel] orden para la tablet: {destino}")


def tomar_orden(ip):
    """La tablet retira la orden pendiente (una sola vez). Vence a los 60 s si nadie la toma."""
    with _candado:
        destino, cuando = _orden["destino"], _orden["cuando"]
        _orden.update(destino=None)
    if not destino:
        return None
    if time.time() - cuando > 60:
        log(f"[tablet {ip}] la orden {destino} vencio sin usarse")
        return None
    # "auto": volver a la rotacion inteligente (sin ?manual=1, asi no queda trabada)
    final = pagina_sugerida() if destino == "auto" else destino
    if not final:
        return None
    log(f"[tablet {ip}] retiro la orden -> va a {final}")
    return final


def generar_apps():
    archivos = sorted(f for f in os.listdir(CARPETA_APK) if f.endswith(".apk")) \
        if os.path.isdir(CARPETA_APK) else []
    filas = "".join(
        f'<a class="app" href="/apk/{urllib.parse.quote(f)}">&darr;&nbsp; {html.escape(f)}</a>'
        for f in archivos
    ) or '<div class="tenue" style="font-size:18px">No hay APKs en la carpeta apk/</div>'
    cabecera = f"""<table class="cab"><tr>
  <td><div class="titulo-apps">Apps para instalar</div>
      <div class="tenue">Toc&aacute; una para bajarla y despu&eacute;s abrila desde Descargas</div></td>
  <td class="der" style="vertical-align:top">{nav("apk")}</td>
</tr></table>"""
    return "Apps", CSS_APPS, "{s0}{s1}", [cabecera, filas]


# ---------- Pomodoro ----------

# (los tiempos POMO_* estan arriba, con el resto de la configuracion)
POMO_FASES = {
    "trabajo": ("Concentraci&oacute;n", "#ff6b5e", POMO_TRABAJO),
    "descanso": ("Descanso", "#1ed760", POMO_DESCANSO),
    "largo": ("Descanso largo", "#4fc3f7", POMO_LARGO),
}
_pomo = {"fase": "trabajo", "corriendo": False, "fin": 0.0, "restante": POMO_TRABAJO, "ciclos": 0}
_pomo_candado = threading.Lock()


def notificar(titulo, texto):
    """Notificacion de Ubuntu (si no se puede, no pasa nada)."""
    try:
        subprocess.Popen(["notify-send", "-a", "Tablet", titulo, texto],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def pomo_restante():
    if _pomo["corriendo"]:
        return max(0.0, _pomo["fin"] - time.time())
    return _pomo["restante"]


def _pomo_pasar_fase(completada):
    """Pasa a la fase siguiente (llamar con el candado tomado)."""
    if _pomo["fase"] == "trabajo":
        if completada:
            _pomo["ciclos"] += 1
        largo = completada and _pomo["ciclos"] % POMO_CADA == 0
        _pomo["fase"] = "largo" if largo else "descanso"
    else:
        _pomo["fase"] = "trabajo"
    dur = POMO_FASES[_pomo["fase"]][2]
    _pomo["restante"] = dur
    _pomo["fin"] = time.time() + dur
    if completada:
        nombre = html.unescape(POMO_FASES[_pomo["fase"]][0])
        notificar(f"Pomodoro: {nombre}", f"{dur // 60} minutos")
        log(f"[pomodoro] empieza: {nombre}")


def pomo_accion(accion):
    with _pomo_candado:
        if accion == "alternar":
            accion = "pausar" if _pomo["corriendo"] else "iniciar"
        if accion == "iniciar" and not _pomo["corriendo"]:
            _pomo["fin"] = time.time() + _pomo["restante"]
            _pomo["corriendo"] = True
        elif accion == "pausar" and _pomo["corriendo"]:
            _pomo["restante"] = max(0.0, _pomo["fin"] - time.time())
            _pomo["corriendo"] = False
        elif accion == "saltar":
            _pomo_pasar_fase(completada=False)
        elif accion == "reiniciar":
            _pomo.update(fase="trabajo", corriendo=False, restante=POMO_TRABAJO, ciclos=0)


def vigilar_pomodoro():
    """Hilo de fondo: cuando termina una fase arranca la siguiente y avisa."""
    while True:
        with _pomo_candado:
            if _pomo["corriendo"] and time.time() >= _pomo["fin"]:
                _pomo_pasar_fase(completada=True)
        time.sleep(1)


def fmt_cuenta(seg):
    seg = int(-(-seg // 1))  # redondeo hacia arriba
    return f"{seg // 60:02d}:{seg % 60:02d}"


def generar_pomodoro():
    nombre, color_fase, dur = POMO_FASES[_pomo["fase"]]
    restante = pomo_restante()
    corriendo = _pomo["corriendo"]
    pct = 100 * (1 - restante / dur) if dur else 0
    hechos = _pomo["ciclos"] % POMO_CADA
    puntos = "".join(
        f'<span class="punto{" lleno" if i < hechos else ""}"></span>' for i in range(POMO_CADA))

    cabecera = f"""<table class="cab"><tr>
  <td class="reloj-chico">{time.strftime("%H:%M")}</td>
  <td class="der">{nav("pomodoro")}</td>
</tr></table>"""

    cuenta = f"""<div class="fase" style="color:{color_fase}">{nombre}{"" if corriendo else
                 ' <span class="pausado">&middot; EN PAUSA</span>'}</div>
<div id="cuenta" class="cuenta" data-corriendo="{1 if corriendo else 0}"
     data-restante="{int(-(-restante // 1))}">{fmt_cuenta(restante)}</div>
<div class="barra-pomo"><div style="width:{pct:.1f}%;background:{color_fase}"></div></div>
<div class="ciclos">{puntos}<span class="tenue">&nbsp; {_pomo["ciclos"]} pomodoro{"" if _pomo["ciclos"] == 1 else "s"} completado{"" if _pomo["ciclos"] == 1 else "s"}</span></div>"""

    def boton(accion, texto, clase=""):
        return (f'<a href="/pomodoro/accion?a={accion}" onclick="return ir(\'/pomodoro/accion?a={accion}\')"'
                f'{" class=" + clase if clase else ""}>{texto}</a>')

    botones = f"""<table class="botones-pomo"><tr>
  <td>{boton("reiniciar", "Reiniciar")}</td>
  <td style="width:44%">{boton("alternar", "Pausar" if corriendo else "Iniciar", "principal")}</td>
  <td>{boton("saltar", "Saltar &raquo;")}</td>
</tr></table>"""
    return "Pomodoro", CSS_POMODORO, "{s0}{s1}{s2}", [cabecera, cuenta, botones]


CSS_POMODORO = """
.cab { width:100%; }
.reloj-chico { font-size:26px; font-weight:bold; color:#c8ccd4; }
.fase { text-align:center; font-size:24px; font-weight:bold; letter-spacing:1px; margin-top:8px; }
.pausado { color:#ffb300; font-size:18px; }
.cuenta { text-align:center; font-size:150px; font-weight:bold; line-height:150px; letter-spacing:-4px;
          margin-top:2px; }
.barra-pomo { background:#262a31; height:8px; margin:6px 60px 0 60px; }
.barra-pomo div { height:8px; }
.ciclos { text-align:center; margin-top:14px; font-size:15px; }
.punto { display:inline-block; width:14px; height:14px; border-radius:7px; margin:0 4px;
         background:#30353e; vertical-align:middle; }
.punto.lleno { background:#ff6b5e; }
.botones-pomo { width:100%; margin-top:18px; }
.botones-pomo td { text-align:center; padding:0 4px; }
.botones-pomo a { display:block; background:#262a31; color:#f2f2f2; text-decoration:none;
                  font-size:20px; height:56px; line-height:56px; }
.botones-pomo a.principal { background:#1db954; color:#0e0f12; font-weight:bold; font-size:24px; }
"""


# ---------- Notas (se escriben en el panel de la PC, se ven en la tablet) ----------

NOTAS_ARCHIVO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notas.json")
NOTAS_MAX = 20
_notas_candado = threading.Lock()


def cargar_notas():
    try:
        with open(NOTAS_ARCHIVO, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


_notas = cargar_notas()


def guardar_notas():
    """Escritura atomica: si se corta la luz justo, no queda el archivo a medias."""
    tmp = NOTAS_ARCHIVO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_notas, f, ensure_ascii=False, indent=1)
    os.replace(tmp, NOTAS_ARCHIVO)


def agregar_nota(texto):
    texto = texto.strip()[:500]
    if not texto:
        return False
    with _notas_candado:
        nuevo_id = max((n["id"] for n in _notas), default=0) + 1
        _notas.insert(0, {"id": nuevo_id, "texto": texto, "creada": time.time()})
        del _notas[NOTAS_MAX:]
        guardar_notas()
    log(f"[notas] nueva: {texto[:40]}")
    return True


def borrar_nota(nota_id):
    with _notas_candado:
        antes = len(_notas)
        if nota_id == "todas":
            _notas.clear()
        else:
            _notas[:] = [n for n in _notas if str(n["id"]) != str(nota_id)]
        if len(_notas) != antes:
            guardar_notas()


def tam_nota(n, largo):
    """Tamano de letra: una nota sola y corta se ve enorme; muchas, mas chicas."""
    if n == 1:
        for limite, tam in ((15, 72), (40, 54), (90, 40), (180, 30)):
            if largo <= limite:
                return tam
        return 24
    if n == 2:
        return 36 if largo <= 60 else 28
    if n <= 4:
        return 26
    return 21


def generar_notas():
    cabecera = f"""<table class="cab"><tr>
  <td class="reloj-chico">{time.strftime("%H:%M")}</td>
  <td class="der">{nav("notas")}</td>
</tr></table>"""
    visibles = _notas[:7]
    if not visibles:
        cuerpo = ('<div class="vacio-notas">No hay notas.<br>'
                  '<span class="tenue">Escribilas desde el panel de la PC:<br>'
                  'localhost:8080/control</span></div>')
    else:
        tam = tam_nota(len(visibles), max(len(n["texto"]) for n in visibles))
        filas = "".join(
            f'<tr><td class="nota" style="font-size:{tam}px;line-height:{int(tam * 1.18)}px">'
            f'{html.escape(n["texto"]).replace(chr(10), "<br>")}</td>'
            f'<td class="listo"><a href="/notas/borrar?id={n["id"]}" '
            f'onclick="return ir(\'/notas/borrar?id={n["id"]}\')">Listo</a></td></tr>'
            for n in visibles
        )
        extra = (f'<div class="tenue mas">y {len(_notas) - len(visibles)} m&aacute;s en el panel</div>'
                 if len(_notas) > len(visibles) else "")
        cuerpo = f'<table class="notas">{filas}</table>{extra}'
    return "Notas", CSS_NOTAS, "{s0}{s1}", [cabecera, cuerpo]


CSS_NOTAS = """
.cab { width:100%; margin-bottom:6px; }
.reloj-chico { font-size:26px; font-weight:bold; color:#c8ccd4; }
.notas { width:100%; }
.notas td { border-bottom:2px solid #1b1d22; vertical-align:middle; }
.nota { font-weight:bold; padding:10px 12px 10px 4px; color:#fff6c8; word-wrap:break-word; }
.listo { width:1%; padding-left:8px; }
.listo a { display:block; background:#262a31; color:#c8ccd4; text-decoration:none; font-size:15px;
           padding:10px 12px; white-space:nowrap; }
.mas { font-size:14px; margin-top:8px; }
.vacio-notas { font-size:30px; text-align:center; margin-top:90px; line-height:44px; }
.vacio-notas .tenue { font-size:18px; line-height:28px; }
"""


PANEL_HTML = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<title>Panel de la tablet</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; background: #0e0f12; color: #f2f2f2; font-family: system-ui, sans-serif; }
  main { max-width: 1240px; margin: 0 auto; padding: 28px 24px; }
  header { display: flex; align-items: center; gap: 16px; margin-bottom: 22px; }
  h1 { font-size: 24px; margin: 0; }
  .estado { font-size: 14px; padding: 6px 12px; border-radius: 999px; background: #262a31; color: #c8ccd4; }
  .estado.ok { background: #12351f; color: #5ee08a; }
  .estado.mal { background: #3a1717; color: #ff8a80; }
  .grilla { display: grid; grid-template-columns: 820px 1fr; gap: 24px; align-items: start; }
  @media (max-width: 1180px) { .grilla { grid-template-columns: 1fr; } }
  .marco { width: 820px; max-width: 100%; background: #1b1d22; border-radius: 14px; padding: 10px; }
  .marco .pantalla { width: 800px; height: 432px; overflow: hidden; border-radius: 4px; background: #000;
                     transform-origin: top left; }
  .marco iframe { width: 800px; height: 432px; border: 0; display: block; }
  .nota { font-size: 13px; color: #9aa0aa; margin-top: 8px; }
  .tarjeta { background: #1b1d22; border-radius: 14px; padding: 18px; margin-bottom: 16px; }
  .tarjeta h2 { font-size: 13px; letter-spacing: 1.5px; text-transform: uppercase; color: #9aa0aa;
                margin: 0 0 12px; font-weight: 600; }
  .botones { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  button { font: inherit; font-size: 16px; border: 0; border-radius: 10px; padding: 14px 10px;
           background: #262a31; color: #f2f2f2; cursor: pointer; }
  button:hover { background: #30353e; }
  button.activo { background: #1db954; color: #0e0f12; font-weight: 600; }
  button.auto { grid-column: span 2; background: transparent; border: 1px solid #30353e; }
  button.auto.activo { border-color: #1db954; color: #1ed760; background: transparent; }
  .cancion { font-size: 18px; font-weight: 600; margin-bottom: 2px; }
  .artista { color: #1ed760; margin-bottom: 14px; }
  .reproductor { display: grid; grid-template-columns: 1fr 2fr 1fr; gap: 8px; }
  .reproductor .principal { background: #1db954; color: #0e0f12; font-weight: 600; }
  .detalle { font-size: 14px; color: #9aa0aa; line-height: 1.6; }
  .izquierda .tarjeta { margin-top: 16px; }
  textarea { width: 100%; font: inherit; font-size: 18px; background: #0e0f12; color: #fff6c8;
             border: 1px solid #30353e; border-radius: 10px; padding: 12px; resize: vertical; }
  textarea:focus { outline: none; border-color: #1db954; }
  .fila { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 10px; }
  .fila label { font-size: 14px; color: #c8ccd4; display: flex; gap: 8px; align-items: center; cursor: pointer; }
  .fila .principal { background: #1db954; color: #0e0f12; font-weight: 600; padding: 12px 26px; }
  #lista-notas { list-style: none; padding: 0; margin: 14px 0 0; }
  #lista-notas li { display: flex; align-items: center; gap: 12px; padding: 10px 0;
                    border-top: 1px solid #262a31; font-size: 16px; color: #fff6c8; }
  #lista-notas li span { flex: 1; white-space: pre-wrap; word-break: break-word; }
  #lista-notas li button { padding: 6px 12px; font-size: 13px; }
  .enlace { background: none; color: #9aa0aa; padding: 6px 0; font-size: 13px; text-decoration: underline; }
  .enlace:hover { background: none; color: #ff8a80; }
  .pomo-cuenta { font-size: 44px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: -1px; }
  .pomo-fase { font-weight: 600; margin-bottom: 2px; }
</style>
</head><body><main>
<header>
  <h1>Panel de la tablet</h1>
  <span id="estado" class="estado">Buscando la tablet&hellip;</span>
</header>
<div class="grilla">
  <div>
    <div class="marco"><div class="pantalla" id="pantalla"><iframe id="vista" src="/?vista=1"></iframe></div></div>
    <div class="nota" id="nota-vista">Vista previa de lo que muestra la tablet.</div>
    <div class="izquierda">
      <section class="tarjeta">
        <h2>Notas para la tablet</h2>
        <textarea id="nota-texto" rows="2" maxlength="500"
                  placeholder="Escrib&iacute; una nota&hellip; (Ctrl+Enter para agregarla)"></textarea>
        <div class="fila">
          <label><input type="checkbox" id="nota-mostrar" checked> Mostrarla ya en la tablet</label>
          <button class="principal" id="nota-agregar">Agregar</button>
        </div>
        <ul id="lista-notas"></ul>
        <button class="enlace" id="notas-borrar-todas" hidden>Borrar todas</button>
      </section>
    </div>
  </div>
  <div>
    <section class="tarjeta">
      <h2>Mostrar en la tablet</h2>
      <div class="botones">
        <button data-ir="monitor" data-pagina="/">Monitor</button>
        <button data-ir="musica" data-pagina="/musica">M&uacute;sica</button>
        <button data-ir="clima" data-pagina="/clima">Clima</button>
        <button data-ir="pomodoro" data-pagina="/pomodoro">Pomodoro</button>
        <button data-ir="notas" data-pagina="/notas">Notas</button>
        <button data-ir="apk" data-pagina="/apk">Apps</button>
        <span></span>
        <button class="auto" id="btn-rotacion">Cambio autom&aacute;tico de p&aacute;ginas</button>
      </div>
      <p class="detalle" id="detalle-auto"></p>
    </section>
    <section class="tarjeta">
      <h2>M&uacute;sica</h2>
      <div class="cancion" id="cancion">&mdash;</div>
      <div class="artista" id="artista"></div>
      <div class="reproductor">
        <button data-accion="previous" aria-label="Anterior">&laquo;</button>
        <button data-accion="play-pause" class="principal" id="btn-pp">Play / Pausa</button>
        <button data-accion="next" aria-label="Siguiente">&raquo;</button>
      </div>
    </section>
    <section class="tarjeta">
      <h2>Pomodoro</h2>
      <div class="pomo-fase" id="pomo-fase">Concentraci&oacute;n</div>
      <div class="pomo-cuenta" id="pomo-cuenta">25:00</div>
      <p class="detalle" id="pomo-ciclos"></p>
      <div class="reproductor">
        <button data-pomo="reiniciar">Reiniciar</button>
        <button data-pomo="alternar" class="principal" id="pomo-btn">Iniciar</button>
        <button data-pomo="saltar">Saltar</button>
      </div>
    </section>
  </div>
</div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let paginaVista = "/";

// Escala la vista previa si la ventana es angosta
function ajustarVista() {
  const marco = document.querySelector(".marco");
  const escala = Math.min(1, (marco.clientWidth - 20) / 800);
  $("pantalla").style.transform = `scale(${escala})`;
  marco.style.height = (432 * escala + 20) + "px";
}
window.addEventListener("resize", ajustarVista);
ajustarVista();

async function refrescar() {
  let d;
  try { d = await (await fetch("/control/estado", { cache: "no-store" })).json(); }
  catch { $("estado").textContent = "El servidor no responde"; $("estado").className = "estado mal"; return; }

  const t = d.tablet;
  if (t.conectada) {
    $("estado").textContent = `Tablet conectada · ${t.ip}`;
    $("estado").className = "estado ok";
  } else {
    $("estado").textContent = t.hace == null ? "La tablet todavía no se conectó"
                                             : `Tablet sin señal hace ${t.hace} s`;
    $("estado").className = "estado mal";
  }

  // Resaltar la pagina que muestra la tablet y sincronizar la vista previa
  const actual = t.conectada ? t.pagina : d.sugerida;
  document.querySelectorAll("[data-pagina]").forEach(b =>
    b.classList.toggle("activo", b.dataset.pagina === actual));
  if (actual && actual !== paginaVista) {
    paginaVista = actual;
    $("vista").src = actual + "?vista=1";
  }
  $("nota-vista").textContent = t.conectada ? "Vista previa de lo que muestra la tablet."
                                            : "La tablet no está conectada: muestra la página que le tocaría.";

  rotacion = d.rotacion_automatica;
  $("btn-rotacion").textContent = "Cambio automático: " + (rotacion ? "activado" : "desactivado");
  $("btn-rotacion").classList.toggle("activo", rotacion);
  $("detalle-auto").textContent = d.orden_pendiente
    ? "Enviando la orden a la tablet…"
    : !rotacion
      ? "Modo fijo: la tablet se queda en la página que elijas, con estos botones o con los de la tablet."
      : d.vuelve_al_monitor_en != null && !(d.musica && d.musica.estado === "Playing")
        ? `Sin música: vuelve al Monitor en ${d.vuelve_al_monitor_en} s.`
        : "En automático: Pomodoro si está corriendo; si no, Música mientras suena algo; si no, va alternando Monitor, Clima y Notas (si hay).";

  pomo = d.pomodoro;
  pomoFin = Date.now() + pomo.restante * 1000;
  $("pomo-fase").textContent = pomo.fase + (pomo.corriendo ? "" : " · en pausa");
  $("pomo-fase").style.color = COLORES_POMO[pomo.clave];
  $("pomo-btn").textContent = pomo.corriendo ? "Pausar" : "Iniciar";
  $("pomo-ciclos").textContent = `${pomo.ciclos} completados`;
  pintarPomodoro();
  pintarNotas(d.notas || []);

  const m = d.musica || {};
  $("cancion").textContent = m.titulo || m.mensaje || "—";
  $("artista").textContent = m.artista || "";
  $("btn-pp").textContent = m.estado === "Playing" ? "Pausa" : "Play";
}

document.querySelectorAll("[data-ir]").forEach(b => b.addEventListener("click", async () => {
  await fetch("/control/ir?pagina=" + b.dataset.ir);
  refrescar();
}));
document.querySelectorAll("[data-accion]").forEach(b => b.addEventListener("click", async () => {
  await fetch("/control?accion=" + b.dataset.accion + "&ajax=1");
  refrescar();
}));

// ----- Modo automatico / fijo -----
let rotacion = true;
$("btn-rotacion").addEventListener("click", async () => {
  await fetch("/control/rotacion?activa=" + (rotacion ? "0" : "1"));
  refrescar();
});

// ----- Pomodoro -----
const COLORES_POMO = { trabajo: "#ff6b5e", descanso: "#1ed760", largo: "#4fc3f7" };
let pomo = null, pomoFin = 0;
function pintarPomodoro() {
  if (!pomo) return;
  const r = pomo.corriendo ? Math.max(0, Math.ceil((pomoFin - Date.now()) / 1000)) : pomo.restante;
  $("pomo-cuenta").textContent =
    String(Math.floor(r / 60)).padStart(2, "0") + ":" + String(r % 60).padStart(2, "0");
}
setInterval(pintarPomodoro, 500);
document.querySelectorAll("[data-pomo]").forEach(b => b.addEventListener("click", async () => {
  await fetch("/control/pomodoro?a=" + b.dataset.pomo);
  refrescar();
}));

// ----- Notas -----
let notasPrevias = "";
function pintarNotas(notas) {
  const firma = JSON.stringify(notas);
  if (firma === notasPrevias) return;  // no redibujar si no cambio nada
  notasPrevias = firma;
  const ul = $("lista-notas");
  ul.replaceChildren(...notas.map(n => {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.textContent = n.texto;  // textContent: el texto nunca se interpreta como HTML
    const b = document.createElement("button");
    b.textContent = "Borrar";
    b.onclick = async () => { await fetch("/control/notas/borrar?id=" + n.id); refrescar(); };
    li.append(span, b);
    return li;
  }));
  $("notas-borrar-todas").hidden = notas.length < 2;
}
async function agregarNota() {
  const texto = $("nota-texto").value.trim();
  if (!texto) return;
  await fetch("/control/notas", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ texto, mostrar: $("nota-mostrar").checked }),
  });
  $("nota-texto").value = "";
  $("nota-texto").focus();
  refrescar();
}
$("nota-agregar").addEventListener("click", agregarNota);
$("nota-texto").addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); agregarNota(); }
});
$("notas-borrar-todas").addEventListener("click", async () => {
  if (!confirm("¿Borrar todas las notas?")) return;
  await fetch("/control/notas/borrar?id=todas");
  refrescar();
});

refrescar();
setInterval(refrescar, 2000);
</script>
</body></html>
"""


CSS_APPS = """
.cab { width:100%; margin-bottom:18px; }
.titulo-apps { font-size:30px; font-weight:bold; }
.app { display:block; background:#1b1d22; color:#1ed760; text-decoration:none;
       font-size:24px; padding:16px 18px; margin-bottom:8px; }
"""

# Carpeta apk/ al lado de este script
CARPETA_APK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apk")


# ---------- Servidor ----------

class Manejador(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(url.query)

        paginas = {"/": generar_html, "/index.html": generar_html,
                   "/musica": generar_musica, "/clima": generar_clima,
                   "/pomodoro": generar_pomodoro, "/notas": generar_notas,
                   "/apk": generar_apps, "/apk/": generar_apps}
        # "Local" = desde esta misma PC, ya sea por localhost o por su IP de red
        # (si el cliente tiene la misma IP que el servidor, es la propia PC).
        cliente = self.client_address[0]
        es_local = cliente in ("127.0.0.1", "::1") or cliente == self.connection.getsockname()[0]

        if url.path in paginas:
            titulo, css, plantilla, secciones = paginas[url.path]()
            if not es_local:
                # La PC sabe que muestra la tablet (lo usa el panel /control)
                pag = url.path.rstrip("/") or "/"
                if "parcial" not in params:
                    log(f"[tablet {cliente}] abrio {pag}{'?' + url.query if url.query else ''}")
                _tablet.update(ip=cliente, pagina=pag, visto=time.time())
            if "parcial" in params:
                # Solo los datos: el JavaScript de la pagina los reparte en cada seccion.
                # Al final va la pagina a mostrar: una orden del panel (con "!") o la sugerida.
                destino = pagina_sugerida()
                v = params.get("v", [None])[0]
                if v != VERSION and (v is not None or not es_local):
                    # Pagina vieja: hay que recargarla para que tome la version nueva.
                    # Con version (script nuevo) se fuerza con "!"; sin version (script de
                    # antes) se usa una direccion distinta a la actual, que ese script sigue.
                    destino = ("!" + url.path) if v is not None else (url.path + "?manual=1")
                    if not es_local:
                        log(f"[tablet {cliente}] tenia una version vieja de la pagina: la recargo")
                elif not es_local:
                    orden = tomar_orden(cliente)
                    if orden:
                        destino = "!" + orden
                self.responder_html(SEPARADOR.join(secciones + [destino]))
            else:
                self.responder_html(pagina(titulo, css, plantilla, secciones))
        elif url.path in ("/pomodoro/accion", "/notas/borrar"):
            # Acciones que tambien se pueden hacer desde la tablet (con el mouse)
            if url.path == "/pomodoro/accion":
                accion = params.get("a", [""])[0]
                if accion in ("iniciar", "pausar", "alternar", "saltar", "reiniciar"):
                    pomo_accion(accion)
                volver = "/pomodoro"
            else:
                borrar_nota(params.get("id", [""])[0])
                volver = "/notas"
            if "ajax" in params:
                self.send_response(204)
            else:
                self.send_response(303)
                self.send_header("Location", volver)
            self.end_headers()
        elif url.path.startswith("/control/") or (url.path == "/control" and "accion" not in params):
            self.panel_de_control(url, params, es_local)
        elif url.path.startswith("/apk/"):
            # basename evita salir de la carpeta (por ejemplo con /apk/../../algo)
            nombre = os.path.basename(urllib.parse.unquote(url.path[len("/apk/"):]))
            ruta = os.path.join(CARPETA_APK, nombre)
            if not nombre.endswith(".apk") or not os.path.isfile(ruta):
                self.send_error(404)
                return
            with open(ruta, "rb") as f:
                datos = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Disposition", f'attachment; filename="{nombre}"')
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos)
        elif url.path == "/icono":
            tipo = params.get("t", [""])[0]
            try:
                tam = max(16, min(256, int(params.get("s", ["64"])[0])))
            except ValueError:
                tam = 64
            if Image is None or tipo not in ("sol", "luna", "solnube", "lunanube", "nube",
                                             "lluvia", "tormenta", "nieve", "niebla"):
                self.send_error(404)
                return
            if (tipo, tam) not in _iconos:
                _iconos[(tipo, tam)] = dibujar_icono(tipo, tam)
            datos = _iconos[(tipo, tam)]
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(datos)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(datos)
        elif url.path == "/portada":
            clave = params.get("k", [""])[0]
            if clave not in _urls_portada:
                self.send_error(404)
                return
            try:
                datos, tipo, _ = bajar_portada(_urls_portada[clave])
            except Exception:
                self.send_error(502)
                return
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(datos)))
            self.send_header("Cache-Control", "max-age=86400")  # que no la baje en cada recarga
            self.end_headers()
            self.wfile.write(datos)
        elif url.path == "/control":
            accion = params.get("accion", [""])[0]
            if accion in ACCIONES:
                try:
                    subprocess.run(["playerctl", "--player", JUGADORES, accion], timeout=2)
                    time.sleep(0.3)  # darle tiempo al reproductor a cambiar de cancion
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
            if "ajax" in params:
                self.send_response(204)  # lo mando el JavaScript: no hace falta redirigir
                self.end_headers()
            else:
                self.send_response(303)
                self.send_header("Location", "/musica")
                self.end_headers()
        else:
            self.send_error(404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        cliente = self.client_address[0]
        es_local = cliente in ("127.0.0.1", "::1") or cliente == self.connection.getsockname()[0]
        if url.path != "/control/notas" or not es_local:
            self.send_error(403 if url.path == "/control/notas" else 404)
            return
        largo = int(self.headers.get("Content-Length") or 0)
        if largo > 10000:
            self.send_error(413)
            return
        try:
            datos = json.loads(self.rfile.read(largo).decode("utf-8"))
        except ValueError:
            self.send_error(400)
            return
        if agregar_nota(str(datos.get("texto", ""))) and datos.get("mostrar"):
            dar_orden("/notas?manual=1")
        self.send_response(204)
        self.end_headers()

    def panel_de_control(self, url, params, es_local):
        # Solo desde la propia PC: nadie mas en tu WiFi puede manejar la tablet
        if not es_local:
            self.send_error(403, "El panel solo se abre desde la PC")
            return
        if url.path in ("/control", "/control/"):
            self.responder_html(PANEL_HTML)
        elif url.path == "/control/ir":
            pag = params.get("pagina", [""])[0]
            destinos = {"monitor": "/?manual=1", "clima": "/clima?manual=1", "musica": "/musica?manual=1",
                        "pomodoro": "/pomodoro?manual=1", "notas": "/notas?manual=1",
                        "apk": "/apk?manual=1", "auto": "auto"}
            if pag not in destinos:
                self.send_error(400)
                return
            dar_orden(destinos[pag])
            self.send_response(204)
            self.end_headers()
        elif url.path == "/control/rotacion":
            _rotacion["activa"] = params.get("activa", ["1"])[0] == "1"
            log(f"[panel] cambio automatico de paginas: {'activado' if _rotacion['activa'] else 'desactivado'}")
            if _rotacion["activa"]:
                dar_orden("auto")  # que la tablet pase ya a la pagina que corresponde
            self.send_response(204)
            self.end_headers()
        elif url.path == "/control/pomodoro":
            accion = params.get("a", [""])[0]
            if accion in ("iniciar", "pausar", "alternar", "saltar", "reiniciar"):
                pomo_accion(accion)
                # Al iniciar desde la PC, que la tablet muestre el pomodoro
                if accion in ("iniciar", "alternar") and _pomo["corriendo"]:
                    dar_orden("/pomodoro?manual=1")
            self.send_response(204)
            self.end_headers()
        elif url.path == "/control/notas/borrar":
            borrar_nota(params.get("id", [""])[0])
            self.send_response(204)
            self.end_headers()
        elif url.path == "/control/estado":
            m = leer_musica()
            sin_musica = time.time() - _ultimo_sonando
            datos = {
                "tablet": {
                    "conectada": time.time() - _tablet["visto"] < 10,
                    "ip": _tablet["ip"], "pagina": _tablet["pagina"],
                    "hace": int(time.time() - _tablet["visto"]) if _tablet["visto"] else None,
                },
                "musica": m if isinstance(m, dict) else {"mensaje": m},
                "sugerida": pagina_sugerida(),
                "rotacion_automatica": _rotacion["activa"],
                "vuelve_al_monitor_en": max(0, int(ESPERA_SIN_MUSICA - sin_musica))
                if pagina_sugerida() == "/musica" else None,
                "orden_pendiente": _orden["destino"],
                "pomodoro": {"fase": html.unescape(POMO_FASES[_pomo["fase"]][0]),
                             "clave": _pomo["fase"], "corriendo": _pomo["corriendo"],
                             "restante": int(-(-pomo_restante() // 1)), "ciclos": _pomo["ciclos"]},
                "notas": _notas,
            }
            cuerpo = json.dumps(datos).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(cuerpo)
        else:
            self.send_error(404)

    def responder_html(self, texto):
        cuerpo = texto.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, *args):
        pass  # sin un log por cada recarga


def ip_local():
    """IP de la PC en la red de tu casa (no manda ningun paquete)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    threading.Thread(target=muestreador, daemon=True).start()
    threading.Thread(target=vigilar_musica, daemon=True).start()
    threading.Thread(target=vigilar_clima, daemon=True).start()
    threading.Thread(target=vigilar_pomodoro, daemon=True).start()
    servidor = ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador)
    print(f"Monitor andando. En la tablet abri:  http://{ip_local()}:{PUERTO}")
    print("Ctrl+C para cortar.")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nChau.")
