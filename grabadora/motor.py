"""Motor de Grabadora: micrófono, Whisper (MLX) e historial. Sin nada de interfaz."""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd

# Abierta desde Finder la app no trae el PATH de la terminal; ffmpeg vive en Homebrew.
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")
# Si el modelo ya está descargado, que no pregunte a internet al abrir. La primera vez sí lo baja (~1.6 GB).
if (Path.home() / ".cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/snapshots").exists():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

MODELO = "mlx-community/whisper-large-v3-turbo"
TASA = 16000
CARPETA = Path.home() / "Documents" / "Grabadora"
INDICE = CARPETA / "grabaciones.json"


ALUCINACIONES = ("amara.org", "subtítulos", "subtitulos", "suscríbete", "gracias por ver", "thanks for watching")


def alucinacion(t):
    t = t.lower()
    return any(a in t for a in ALUCINACIONES)


AJUSTES = CARPETA / "ajustes.json"


def ajuste(clave, defecto=None):
    try:
        return json.loads(AJUSTES.read_text()).get(clave, defecto)
    except Exception:
        return defecto


def guardar_ajuste(clave, valor):
    try:
        d = json.loads(AJUSTES.read_text())
    except Exception:
        d = {}
    d[clave] = valor
    AJUSTES.parent.mkdir(parents=True, exist_ok=True)
    AJUSTES.write_text(json.dumps(d, ensure_ascii=False, indent=1))


def cargar_indice():
    try:
        return json.loads(INDICE.read_text())
    except Exception:
        return []


class Api:
    def __init__(self):
        CARPETA.mkdir(parents=True, exist_ok=True)
        self._emisor = None      # fn(nombre, dato): lo pone la app para avisar a las vistas
        self._app = None         # la app nativa (panel, ventana, diálogos)
        self._items = cargar_indice()
        self._lock = threading.Lock()
        self._modelo_listo = False
        self._trozos = []
        self._stream = None
        self._inicio = 0.0
        self._nivel_actual = 0.0
        self._reproductor = None
        self._cola = queue.Queue()
        # Transcripción en vivo mientras se graba.
        self._sesion = 0
        self._idioma_vivo = ""
        self._corte = 0          # muestra donde empieza lo que aún no se fija
        self._fijo = ""          # texto ya fijado (pedazos cerrados en una pausa)
        self._tentativo = ""     # lo último, que todavía puede cambiar
        self._ultimo_vivo = 0.0
        # Todo lo de MLX corre en un solo hilo: MLX no se lleva bien con varios.
        threading.Thread(target=self._trabajador, daemon=True).start()

    # ---------- utilidades internas ----------
    def _guardar(self):
        with self._lock:
            tmp = INDICE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._items, ensure_ascii=False, indent=1))
            tmp.replace(INDICE)

    def _buscar(self, id_):
        return next((x for x in self._items if x["id"] == id_), None)

    def _avisar(self, fn, dato):
        if self._emisor:
            try:
                self._emisor(fn, dato)
            except Exception as e:  # noqa: BLE001
                print("avisar:", e)

    def _trabajador(self):
        import mlx_whisper

        try:
            mlx_whisper.transcribe(np.zeros(TASA, dtype=np.float32), path_or_hf_repo=MODELO, language="es")
        except Exception as e:  # noqa: BLE001
            print("No se pudo precargar el modelo:", e)
        self._modelo_listo = True
        self._avisar("modeloListo", True)

        while True:
            if self._stream is not None and time.time() - self._ultimo_vivo > 0.4:
                try:
                    self._paso_vivo(mlx_whisper)
                except Exception as e:  # noqa: BLE001
                    print("vivo:", e)
                self._ultimo_vivo = time.time()
            try:
                id_ = self._cola.get(timeout=0.1)
            except queue.Empty:
                continue
            item = self._buscar(id_)
            if not item:
                continue
            try:
                t0 = time.time()
                r = mlx_whisper.transcribe(
                    item["audio"],
                    path_or_hf_repo=MODELO,
                    language=item.get("idioma") or None,
                    condition_on_previous_text=False,
                )
                partes = [s["text"].strip() for s in r.get("segments", []) if s["text"].strip() and not alucinacion(s["text"])]
                item["texto"] = "\n".join(partes) if partes else r.get("text", "").strip()
                item["idioma"] = r.get("language") or item.get("idioma")
                item["estado"] = "listo"
                item["tardo"] = round(time.time() - t0, 1)
                Path(item["audio"]).with_suffix(".txt").write_text(item["texto"])
            except Exception as e:  # noqa: BLE001
                item["estado"] = "error"
                item["error"] = str(e)[:300]
            self._guardar()
            self._avisar("transcrito", item)

    def _paso_vivo(self, mlx_whisper):
        sesion = self._sesion
        trozos = list(self._trozos)
        if not trozos:
            return
        audio = np.concatenate(trozos)
        pend = audio[self._corte:]
        if len(pend) < TASA * 0.6:
            return
        rms = lambda x: float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0  # noqa: E731
        general, cola = rms(pend), rms(pend[-int(TASA * 0.5):])
        pausa = cola < 0.012 and cola < general * 0.5
        # Se fija el pedazo al hacer una pausa, o si ya va muy largo.
        fijar = (len(pend) > TASA * 3 and pausa) or len(pend) > TASA * 15
        if general < 0.004:  # puro silencio: Whisper inventa frases con eso
            texto = ""
        else:
            r = mlx_whisper.transcribe(
                pend,
                path_or_hf_repo=MODELO,
                language=self._idioma_vivo or None,
                condition_on_previous_text=False,
                initial_prompt=self._fijo[-200:] or None,
            )
            texto = " ".join(
                s["text"].strip() for s in r.get("segments", [])
                if s["text"].strip() and s.get("no_speech_prob", 0) < 0.6 and not alucinacion(s["text"])
            ).strip()
        if sesion != self._sesion:
            return
        if fijar:
            self._fijo = (self._fijo + " " + texto).strip()
            self._tentativo = ""
            self._corte = len(audio)
        else:
            self._tentativo = texto
        self._avisar("vivo", {"fijo": self._fijo, "tentativo": self._tentativo})

    def _nuevo(self, audio, duracion, idioma, origen, texto=""):
        item = {
            "id": Path(audio).stem,
            "fecha": datetime.now().isoformat(timespec="seconds"),
            "duracion": round(duracion, 1),
            "idioma": idioma or "",
            "audio": str(audio),
            "origen": origen,
            "texto": texto,
            "estado": "transcribiendo",
        }
        with self._lock:
            self._items.insert(0, item)
        self._guardar()
        self._cola.put(item["id"])
        return item

    # ---------- lo que llama la interfaz ----------
    def estado(self):
        return {"modelo_listo": self._modelo_listo, "carpeta": str(CARPETA)}

    def lista(self):
        return self._items

    def empezar(self, idioma=""):
        if self._stream:
            return {"ok": True}
        self._sesion += 1
        self._idioma_vivo = idioma
        self._corte, self._fijo, self._tentativo = 0, "", ""
        self._trozos = []
        self._nivel_actual = 0.0

        def cb(indata, frames, t, status):
            self._trozos.append(indata[:, 0].copy())
            self._nivel_actual = float(np.sqrt(np.mean(indata[:, 0] ** 2)))

        try:
            self._stream = sd.InputStream(samplerate=TASA, channels=1, dtype="float32", callback=cb)
            self._stream.start()
        except Exception as e:  # noqa: BLE001
            self._stream = None
            return {"ok": False, "error": f"No pude abrir el micrófono: {e}"}
        self._inicio = time.time()
        self._avisar("grabacion", True)
        return {"ok": True}

    def nivel(self):
        return {"nivel": self._nivel_actual, "segundos": time.time() - self._inicio if self._stream else 0}

    def detener(self, idioma=""):
        if not self._stream:
            return None
        self._stream.stop()
        self._stream.close()
        self._stream = None
        self._sesion += 1
        self._avisar("grabacion", False)
        vivo = (self._fijo + " " + self._tentativo).strip()
        audio = np.concatenate(self._trozos) if self._trozos else np.zeros(0, dtype=np.float32)
        self._trozos = []
        if len(audio) < TASA * 0.5:
            return {"descartado": True}
        nombre = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        ruta = CARPETA / f"{nombre}.wav"
        with wave.open(str(ruta), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TASA)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        return self._nuevo(ruta, len(audio) / TASA, idioma, "grabado", vivo)

    def cancelar(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._sesion += 1
        self._trozos = []
        self._avisar("grabacion", False)
        return True

    def importar(self, idioma=""):
        rutas = self._app.elegir_audios() if self._app else []
        nuevos = []
        for r in rutas or []:
            origen = Path(r)
            nombre = datetime.now().strftime("%Y-%m-%d %H-%M-%S") + "-" + uuid.uuid4().hex[:4]
            destino = CARPETA / f"{nombre}{origen.suffix.lower()}"
            shutil.copy2(origen, destino)
            dur = 0.0
            try:
                out = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(destino)],
                    capture_output=True, text=True, timeout=20,
                )
                dur = float(out.stdout.strip() or 0)
            except Exception:
                pass
            item = self._nuevo(destino, dur, idioma, origen.name)
            nuevos.append(item)
        return nuevos

    def editar(self, id_, texto):
        item = self._buscar(id_)
        if item:
            item["texto"] = texto
            self._guardar()
            Path(item["audio"]).with_suffix(".txt").write_text(texto)
        return True

    def renombrar(self, id_, titulo):
        item = self._buscar(id_)
        if item:
            item["titulo"] = titulo.strip()
            self._guardar()
        return True

    def retranscribir(self, id_, idioma=""):
        item = self._buscar(id_)
        if item:
            item["estado"] = "transcribiendo"
            item["idioma"] = idioma
            self._guardar()
            self._cola.put(id_)
        return item

    def borrar(self, id_):
        item = self._buscar(id_)
        if not item:
            return False
        for p in (Path(item["audio"]), Path(item["audio"]).with_suffix(".txt")):
            try:
                subprocess.run(["osascript", "-e", f'tell app "Finder" to delete POSIX file "{p}"'],
                               capture_output=True, timeout=10) if p.exists() else None
            except Exception:
                pass
        with self._lock:
            self._items = [x for x in self._items if x["id"] != id_]
        self._guardar()
        return True

    def copiar(self, texto):
        subprocess.run(["pbcopy"], input=texto.encode("utf-8"))
        return True

    def reproducir(self, id_):
        self.parar()
        item = self._buscar(id_)
        if item and Path(item["audio"]).exists():
            self._reproductor = subprocess.Popen(["afplay", item["audio"]])
            threading.Thread(target=self._fin_reproduccion, args=(self._reproductor, id_), daemon=True).start()
            return True
        return False

    def _fin_reproduccion(self, p, id_):
        p.wait()
        if self._reproductor is p:
            self._reproductor = None
            self._avisar("finReproduccion", id_)

    def parar(self):
        if self._reproductor and self._reproductor.poll() is None:
            self._reproductor.terminate()
        self._reproductor = None
        return True

    def finder(self, id_=None):
        item = self._buscar(id_) if id_ else None
        if item:
            subprocess.run(["open", "-R", item["audio"]])
        else:
            subprocess.run(["open", str(CARPETA)])
        return True

    def grabando(self):
        return self._stream is not None

    # ---------- lo que pide la interfaz a la app nativa ----------
    def cerrar_panel(self):
        self._app and self._app.cerrar_panel()
        return True

    def abrir_ventana(self, id_=None):
        self._app and self._app.abrir_ventana(id_)
        return True

    def arrastrar_panel(self, dx, dy):
        self._app and self._app.arrastrar_panel(dx, dy)
        return True

    def soltar_panel(self):
        self._app and self._app.soltar_panel()
        return True

    def tamano_panel(self, w, h):
        self._app and self._app.tamanoPanel(w, h)
        return True
