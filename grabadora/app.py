"""Grabadora: app nativa de Mac (AppKit + Liquid Glass) con el motor de Whisper en motor.py.

- Ventana principal: historial, grabar, subir audio.
- Panel de vidrio a la derecha con ⌃⌥⌘G desde cualquier app: graba, va escribiendo y copias.
"""

import ctypes
import json
import sys
import threading
from pathlib import Path

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSAnimationContext,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSColor,
    NSImage,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSOpenPanel,
    NSPanel,
    NSScreen,
    NSToolbar,
    NSTrackingArea,
    NSView,
    NSVisualEffectView,
    NSWindow,
)
from Foundation import NSURL, NSBundle, NSDistributedNotificationCenter, NSObject
from PyObjCTools import AppHelper
from Quartz import CAMediaTimingFunction
from WebKit import WKUserContentController, WKUserScript, WKWebView, WKWebViewConfiguration

import motor

AQUI = Path(__file__).resolve().parent
UI = AQUI / "ui"

# ---------- estilos de ventana ----------
TITLED, CLOSABLE, MINI, RESIZABLE = 1 << 0, 1 << 1, 1 << 2, 1 << 3
FULL_CONTENT = 1 << 15
NONACTIVATING = 1 << 7
BORDERLESS = 0
ALL_SPACES, FULLSCREEN_AUX = 1 << 0, 1 << 8
FLOATING_LEVEL = 3
SIZE_ALL = 2 | 16  # ancho y alto flexibles

try:
    GlassView = objc.lookUpClass("NSGlassEffectView")
except objc.error:
    GlassView = None


def vidrio(frame, radio, estilo=0):
    """Liquid Glass de macOS 26; en sistemas viejos, el desenfoque clásico."""
    if GlassView is not None:
        v = GlassView.alloc().initWithFrame_(frame)
        v.setCornerRadius_(radio)
        v.setStyle_(estilo)
        return v
    v = NSVisualEffectView.alloc().initWithFrame_(frame)
    v.setMaterial_(13)  # hudWindow
    v.setBlendingMode_(0)
    v.setState_(1)
    v.setWantsLayer_(True)
    v.layer().setCornerRadius_(radio)
    v.layer().setMasksToBounds_(True)
    return v


# ---------- puente JS ⇄ Python ----------
PUENTE_JS = """
(() => {
  let n = 0; const pend = {};
  window.__r = (id, ok, val) => { const p = pend[id]; if (!p) return; delete pend[id]; ok ? p[0](val) : p[1](new Error(val)); };
  window.api = new Proxy({}, { get: (_, m) => (...a) => new Promise((res, rej) => {
    const id = ++n; pend[id] = [res, rej];
    window.webkit.messageHandlers.api.postMessage(JSON.stringify({ id, m, a }));
  }) });
  window.dispatchEvent(new Event('apiready'));
})();
"""

EN_HILO_PRINCIPAL = {"importar", "cerrar_panel", "abrir_ventana", "arrastrar_panel", "soltar_panel", "tamano_panel"}


class Puente(NSObject, protocols=[objc.protocolNamed("WKScriptMessageHandler")]):
    def initWithApi_(self, api):
        self = objc.super(Puente, self).init()
        self.api = api
        return self

    def userContentController_didReceiveScriptMessage_(self, ctrl, msg):
        datos = json.loads(str(msg.body()))
        vista = msg.webView()
        id_, metodo, args = datos["id"], datos["m"], datos.get("a", [])

        def correr():
            try:
                if metodo.startswith("_") or not hasattr(self.api, metodo):
                    raise AttributeError(f"no existe {metodo}")
                res, ok = getattr(self.api, metodo)(*args), True
            except Exception as e:  # noqa: BLE001
                res, ok = str(e), False
            js = f"window.__r({id_}, {'true' if ok else 'false'}, {json.dumps(res, ensure_ascii=False, default=str)})"
            AppHelper.callAfter(vista.evaluateJavaScript_completionHandler_, js, None)

        if metodo in EN_HILO_PRINCIPAL:
            correr()
        else:
            threading.Thread(target=correr, daemon=True).start()


def nueva_vista(api, html, frame):
    conf = WKWebViewConfiguration.alloc().init()
    ctrl = WKUserContentController.alloc().init()
    ctrl.addScriptMessageHandler_name_(Puente.alloc().initWithApi_(api), "api")
    ctrl.addUserScript_(WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(PUENTE_JS, 0, True))
    conf.setUserContentController_(ctrl)
    wv = WKWebView.alloc().initWithFrame_configuration_(frame, conf)
    wv.setValue_forKey_(False, "drawsBackground")  # transparente: se ve el vidrio
    wv.setAutoresizingMask_(SIZE_ALL)
    url = NSURL.fileURLWithPath_(str(UI / html))
    wv.loadFileURL_allowingReadAccessToURL_(url, NSURL.fileURLWithPath_(str(UI)))
    return wv


# ---------- panel flotante ----------
class PanelVidrio(NSPanel):
    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return False


def flotante(p):
    p.setOpaque_(False)
    p.setBackgroundColor_(NSColor.clearColor())
    p.setLevel_(FLOATING_LEVEL)
    p.setCollectionBehavior_(ALL_SPACES | FULLSCREEN_AUX)
    p.setHidesOnDeactivate_(False)
    p.setReleasedWhenClosed_(False)


class Zona(NSView):
    """Avisa cuando el mouse entra/sale (aunque la app esté en segundo plano). Con al_clic, también recibe clics;
    sin él, deja pasar los clics a lo que tiene abajo."""

    def updateTrackingAreas(self):
        for t in list(self.trackingAreas()):
            self.removeTrackingArea_(t)
        opciones = 0x01 | 0x80 | 0x200  # entra/sale, siempre activo, rect visible
        self.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(self.bounds(), opciones, self, None))
        objc.super(Zona, self).updateTrackingAreas()

    def mouseEntered_(self, ev):
        fn = getattr(self, "al_entrar", None)
        fn and fn()

    def mouseExited_(self, ev):
        fn = getattr(self, "al_salir", None)
        fn and fn()

    def hitTest_(self, punto):
        v = objc.super(Zona, self).hitTest_(punto)
        if v is self and not getattr(self, "al_clic", None):
            return None  # sin clic propio: que lo reciba lo de abajo
        return v

    def mouseDown_(self, ev):
        fn = getattr(self, "al_clic", None)
        fn and fn()

    def acceptsFirstMouse_(self, ev):
        return True


def vidrio_oscuro(frame, radio, estilo=1, alfa=0.04):
    """Por defecto «clear» casi sin tinte (la barrita); la tarjeta usa regular + más tinte para leerse mejor."""
    v = vidrio(frame, radio, estilo=estilo)
    if GlassView is not None:
        v.setTintColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(0.0, 0.0, 0.0, alfa))
    return v


# ---------- atajo global (Carbon, no pide permisos de accesibilidad) ----------
class Atajo:
    def __init__(self, codigo, modificadores, fn):
        self.fn = fn
        c = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")

        class HotKeyID(ctypes.Structure):
            _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

        class TypeSpec(ctypes.Structure):
            _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

        Handler = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        c.GetApplicationEventTarget.restype = ctypes.c_void_p
        c.InstallEventHandler.argtypes = [ctypes.c_void_p, Handler, ctypes.c_uint32, ctypes.POINTER(TypeSpec),
                                          ctypes.c_void_p, ctypes.c_void_p]
        c.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, HotKeyID, ctypes.c_void_p,
                                          ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]

        def manejar(_llamada, _evento, _dato):
            try:
                self.fn()
            except Exception as e:  # noqa: BLE001
                print("atajo:", e)
            return 0

        self._handler = Handler(manejar)  # que no se lo lleve el recolector
        objetivo = c.GetApplicationEventTarget()
        tipo = TypeSpec(int.from_bytes(b"keyb", "big"), 5)  # kEventHotKeyPressed
        c.InstallEventHandler(objetivo, self._handler, 1, ctypes.byref(tipo), None, None)
        self._ref = ctypes.c_void_p()
        err = c.RegisterEventHotKey(codigo, modificadores, HotKeyID(int.from_bytes(b"grab", "big"), 1),
                                    objetivo, 0, ctypes.byref(self._ref))
        self.ok = err == 0
        if not self.ok:
            print("No se pudo registrar el atajo, error", err)


CMD, OPT, CTRL = 0x100, 0x800, 0x1000
TECLA_G = 5


# ---------- la app ----------
class App(NSObject):
    def initConApi_(self, api):
        self = objc.super(App, self).init()
        self.api = api
        self.vistas = []
        return self

    # ventana principal
    @objc.python_method
    def crearVentana(self):
        pant = NSScreen.mainScreen().visibleFrame()
        w, h = 1060, 700
        x = pant.origin.x + (pant.size.width - w) / 2
        y = pant.origin.y + (pant.size.height - h) / 2
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h), TITLED | CLOSABLE | MINI | RESIZABLE | FULL_CONTENT, NSBackingStoreBuffered, False)
        win.setTitle_("Grabadora")
        win.setTitlebarAppearsTransparent_(True)
        win.setTitleVisibility_(1)
        win.setMinSize_((760, 520))
        win.setReleasedWhenClosed_(False)
        win.setDelegate_(self)
        win.setFrameAutosaveName_("GrabadoraVentana")
        # Barra de título alta (como Finder en macOS 26): los semáforos caen dentro de la barra lateral.
        barra = NSToolbar.alloc().initWithIdentifier_("grabadora")
        barra.setShowsBaselineSeparator_(False)
        win.setToolbar_(barra)
        win.setToolbarStyle_(3)  # unified
        win.setTitlebarSeparatorStyle_(1)  # sin línea

        fondo = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        fondo.setMaterial_(13)  # hudWindow: el más translúcido
        fondo.setBlendingMode_(0)  # detrás de la ventana: se transparenta el escritorio
        fondo.setState_(1)
        win.setContentView_(fondo)

        # Barra lateral de Liquid Glass flotando adentro, como en macOS 26.
        lateral = vidrio(NSMakeRect(8, 8, 268, h - 16), 18)
        lateral.setAutoresizingMask_(16)  # alto flexible
        fondo.addSubview_(lateral)

        wv = nueva_vista(self.api, "ventana.html", NSMakeRect(0, 0, w, h))
        fondo.addSubview_(wv)
        self.vistas.append(wv)
        self.ventana = win

    @objc.python_method
    def abrir_ventana(self, id_=None):
        NSApp.activateIgnoringOtherApps_(True)
        self.ventana.makeKeyAndOrderFront_(None)
        if id_:
            self.js(self.vistas[0], f"window.abrirNota && window.abrirNota({json.dumps(id_)})")

    def windowShouldClose_(self, win):
        win.orderOut_(None)  # cerrar = esconder; el atajo sigue vivo
        return False

    # ---------- píldora del micrófono + tarjeta del dictado ----------
    # Como la referencia de Bruno: una píldora de vidrio oscuro en la orilla con un anillo; al pasar el mouse
    # (o con ⌃⌥⌘G, o clic) sale a un lado una tarjeta con el texto en vivo.
    BW, BH = 58, 80          # píldora
    CW, CH = 384, 470        # tarjeta
    MARGEN, HUECO = 8, 12

    @objc.python_method
    def crearPanel(self):
        self.lado = motor.ajuste("lado", "derecha")
        self.abierto = False
        self.fijado = False
        self.sobre = {"burbuja": False, "tarjeta": False}
        self.ancla = None  # (x de la orilla derecha de la tarjeta, y del centro) que manda Atajos
        self.burbuja = None
        self.CW, self.CH = 330, 60  # arranca como barrita; panel.html pide otro tamaño al terminar

        # tarjeta
        p = PanelVidrio.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.CW, self.CH), BORDERLESS | NONACTIVATING, NSBackingStoreBuffered, False)
        flotante(p)
        p.setHasShadow_(False)  # la sombra de la ventana dibujaba un marco rectangular; el vidrio trae la suya
        cont = Zona.alloc().initWithFrame_(NSMakeRect(0, 0, self.CW, self.CH))
        cont.al_entrar = lambda: self.hover("tarjeta", True)
        cont.al_salir = lambda: self.hover("tarjeta", False)
        self.vidrio_panel = vidrio_oscuro(NSMakeRect(0, 0, self.CW, self.CH), 30, estilo=0, alfa=0.34)
        self.vidrio_panel.setAutoresizingMask_(SIZE_ALL)
        cont.addSubview_(self.vidrio_panel)
        wv = nueva_vista(self.api, "panel.html", NSMakeRect(0, 0, self.CW, self.CH))
        cont.addSubview_(wv)
        p.setContentView_(cont)
        self.panel, self.vista_panel = p, wv
        self.vistas.append(wv)
        wv.setWantsLayer_(True)
        self.redondear(self.CH)

        # Píldora propia en la orilla: apagada desde que el dictado vive en la barrita de Atajos (⌃⌥⌘A).
        if motor.ajuste("pildora", False):
            self.crearBurbuja()
        self.acomodar()
        cont.updateTrackingAreas()
        NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "avisoDictado:", "com.vareliuu.dictado", None)

    @objc.python_method
    def crearBurbuja(self):
        b = PanelVidrio.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.BW, self.BH), BORDERLESS | NONACTIVATING, NSBackingStoreBuffered, False)
        flotante(b)
        b.setHasShadow_(True)
        bc = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, self.BW, self.BH))
        bc.addSubview_(vidrio_oscuro(NSMakeRect(0, 0, self.BW, self.BH), self.BW / 2))
        bv = nueva_vista(self.api, "burbuja.html", NSMakeRect(0, 0, self.BW, self.BH))
        bc.addSubview_(bv)
        zona = Zona.alloc().initWithFrame_(NSMakeRect(0, 0, self.BW, self.BH))
        zona.al_entrar = lambda: self.hover("burbuja", True)
        zona.al_salir = lambda: self.hover("burbuja", False)
        zona.al_clic = self.clicBurbuja
        bc.addSubview_(zona)
        b.setContentView_(bc)
        self.burbuja = b
        self.vistas.append(bv)

        self.burbuja.orderFrontRegardless()
        zona.updateTrackingAreas()

    @objc.python_method
    def pantalla(self):
        return NSScreen.mainScreen() or NSScreen.screens()[0]

    @objc.python_method
    def rects(self):
        f, v = self.pantalla().frame(), self.pantalla().visibleFrame()
        if self.ancla:  # junto al ícono de Dictado de la barrita de Atajos, en la pantalla de la barrita
            x, yc = self.ancla
            for sc in NSScreen.screens():
                r = sc.frame()
                if r.origin.x - 80 <= x <= r.origin.x + r.size.width + 80 and r.origin.y <= yc <= r.origin.y + r.size.height:
                    v = sc.visibleFrame()
                    break
            ch = min(self.CH, v.size.height - 16)
            cy = max(v.origin.y + 8, min(yc - ch / 2, v.origin.y + v.size.height - 8 - ch))
            if getattr(self, "ancla_lado", "derecha") == "izquierda":  # barrita en la orilla izquierda (⌃⌥⌘Q)
                return None, NSMakeRect(x, cy, self.CW, ch), NSMakeRect(x - 16, cy, self.CW, ch)
            cx = x - self.CW
            return None, NSMakeRect(cx, cy, self.CW, ch), NSMakeRect(cx + 16, cy, self.CW, ch)
        by = v.origin.y + v.size.height * 0.08
        ch = min(self.CH, v.size.height - (by - v.origin.y) - 20)
        if self.lado == "derecha":
            bx = f.origin.x + f.size.width - self.BW - self.MARGEN
            cx = bx - self.HUECO - self.CW
            fuera = 16
        else:
            bx = f.origin.x + self.MARGEN
            cx = bx + self.BW + self.HUECO
            fuera = -16
        burbuja = NSMakeRect(bx, by, self.BW, self.BH)
        tarjeta = NSMakeRect(cx, by, self.CW, ch)
        escondida = NSMakeRect(cx + fuera, by, self.CW, ch)  # se asoma desde la píldora
        return burbuja, tarjeta, escondida

    @objc.python_method
    def acomodar(self):
        burbuja, _, _ = self.rects()
        if self.burbuja is not None and burbuja is not None:
            self.burbuja.setFrame_display_(burbuja, True)
        self.js(self.vista_panel, f"document.body.dataset.lado = {json.dumps(self.lado)}")

    @objc.python_method
    def animar(self, ventana, rect, alfa=None, despues=None, dur=0.38):
        def grupo(ctx):
            ctx.setDuration_(dur)
            ctx.setTimingFunction_(CAMediaTimingFunction.functionWithControlPoints____(0.16, 1, 0.3, 1))
            ventana.animator().setFrame_display_(rect, True)
            if alfa is not None:
                ventana.animator().setAlphaValue_(alfa)

        def fin():  # AppKit espera void
            ventana.invalidateShadow()  # la sombra sigue la forma nueva (si no, quedan esquinas rectas)
            if despues:
                despues()
        NSAnimationContext.runAnimationGroup_completionHandler_(grupo, fin)

    @objc.python_method
    def tamanoPanel(self, w, h):
        w, h = float(w), float(h)
        if abs(w - self.CW) < 1 and abs(h - self.CH) < 1:
            return
        self.CW, self.CH = w, h
        self.redondear(h)
        _, tarjeta, _ = self.rects()
        if self.abierto:
            self.animar(self.panel, tarjeta, dur=0.34)
        else:
            self.panel.setFrame_display_(tarjeta, False)
            self.panel.invalidateShadow()

    @objc.python_method
    def redondear(self, h):
        """Mismas esquinas para el vidrio y para el contenido: la barrita es píldora, la tarjeta 22 px."""
        r = h / 2 if h <= 64 else 22.0
        if GlassView is not None:
            self.vidrio_panel.setCornerRadius_(r)
        capa = self.vista_panel.layer()
        capa.setCornerRadius_(r)
        capa.setMasksToBounds_(True)

    @objc.python_method
    def mostrarPanel(self, grabar=True, fijar=True):
        _, tarjeta, escondida = self.rects()
        if fijar:
            self.fijado = True
        if not self.abierto:
            self.abierto = True
            self.acomodar()
            self.panel.setFrame_display_(escondida, False)
            self.panel.setAlphaValue_(0.0)
            self.panel.orderFrontRegardless()
            self.animar(self.panel, tarjeta, alfa=1.0)
        if fijar:
            self.panel.makeKeyWindow()
        self.js(self.vista_panel, f"window.alAbrir && window.alAbrir({'true' if grabar else 'false'})")

    @objc.python_method
    def cerrar_panel(self):
        self.fijado = False
        if not self.abierto:
            return
        self.abierto = False
        _, _, escondida = self.rects()

        def fin():
            if not self.abierto:
                self.panel.orderOut_(None)
        self.animar(self.panel, escondida, alfa=0.0, despues=fin, dur=0.24)

    @objc.python_method
    def hover(self, donde, dentro):
        self.sobre[donde] = dentro
        if dentro and donde == "burbuja" and not self.abierto:
            self.mostrarPanel(grabar=False, fijar=False)
        if not dentro:
            AppHelper.callLater(0.35, self.quizaEsconder)

    @objc.python_method
    def quizaEsconder(self):
        if self.abierto and not self.fijado and not any(self.sobre.values()) and not self.api.grabando():
            self.cerrar_panel()

    @objc.python_method
    def clicBurbuja(self):
        self.atajo()

    @objc.python_method
    def arrastrar_panel(self, dx, dy):
        self.fijado = True
        f = self.panel.frame()
        self.panel.setFrameOrigin_((f.origin.x + dx, f.origin.y - dy))

    @objc.python_method
    def soltar_panel(self):
        f = self.panel.frame()
        pf = self.pantalla().frame()
        self.lado = "izquierda" if f.origin.x + f.size.width / 2 < pf.origin.x + pf.size.width / 2 else "derecha"
        motor.guardar_ajuste("lado", self.lado)
        if self.burbuja is None:
            return  # sin píldora: la tarjeta se queda donde la sueltes
        burbuja, tarjeta, _ = self.rects()
        self.js(self.vista_panel, f"document.body.dataset.lado = {json.dumps(self.lado)}")
        self.animar(self.burbuja, burbuja)
        self.animar(self.panel, tarjeta)

    @objc.python_method
    def atajo(self):
        if not self.abierto:
            self.mostrarPanel(grabar=True)
        else:
            self.fijado = True
            self.panel.makeKeyWindow()
            self.js(self.vista_panel, "window.atajo && window.atajo()")

    @objc.python_method
    def marcarGrabando(self, si):
        if si:
            self.fijado = True
        self.avisarEstado()

    @objc.python_method
    def avisarEstado(self):
        info = {"grabando": self.api.grabando(), "inicio": float(self.api._inicio if self.api.grabando() else 0)}
        NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
            "com.vareliuu.dictado.estado", None, info, True)

    def avisoDictado_(self, aviso):
        d = dict(aviso.userInfo() or {})
        accion = d.get("accion")
        if "x" in d and "y" in d:
            self.ancla = (float(d["x"]), float(d["y"]))
            self.ancla_lado = d.get("lado", "derecha")
        if accion == "hover":
            self.sobre["burbuja"] = True
            if not self.abierto:
                self.mostrarPanel(grabar=False, fijar=False)
            elif not self.fijado:
                _, tarjeta, _ = self.rects()
                self.animar(self.panel, tarjeta, dur=0.25)
        elif accion == "salir":
            self.hover("burbuja", False)
        elif accion == "alternar":
            self.atajo()
        elif accion == "cerrar":
            self.ancla = None
            self.sobre["burbuja"] = False
            self.cerrar_panel()
        elif accion == "estado":
            self.avisarEstado()

    # utilidades
    @objc.python_method
    def js(self, vista, codigo):
        vista.evaluateJavaScript_completionHandler_(codigo, None)

    @objc.python_method
    def emitir(self, fn, dato):
        if fn == "grabacion":
            AppHelper.callAfter(self.marcarGrabando, bool(dato))
        codigo = f"window.{fn} && window.{fn}({json.dumps(dato, ensure_ascii=False)})"

        def todas():
            for v in self.vistas:
                self.js(v, codigo)

        AppHelper.callAfter(todas)

    @objc.python_method
    def elegir_audios(self):
        p = NSOpenPanel.openPanel()
        p.setAllowsMultipleSelection_(True)
        p.setCanChooseDirectories_(False)
        p.setAllowedFileTypes_(["mp3", "m4a", "wav", "aac", "ogg", "opus", "flac", "mp4", "mov", "webm", "caf", "aiff"])
        p.setMessage_("Elige audios o videos para transcribir")
        if p.runModal() == 1:
            return [str(u.path()) for u in p.URLs()]
        return []

    # ciclo de vida
    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visibles):
        self.abrir_ventana()
        return True

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return False

    def mostrarVentana_(self, _):
        self.abrir_ventana()

    def grabarConAtajo_(self, _):
        self.atajo()


def menus(app_obj):
    barra = NSMenu.alloc().init()

    def menu(titulo, items):
        m = NSMenu.alloc().initWithTitle_(titulo)
        for t, accion, tecla, *objetivo in items:
            if t == "-":
                m.addItem_(NSMenuItem.separatorItem())
                continue
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(t, accion, tecla)
            if objetivo:
                it.setTarget_(objetivo[0])
            m.addItem_(it)
        raiz = NSMenuItem.alloc().init()
        raiz.setSubmenu_(m)
        barra.addItem_(raiz)

    menu("Grabadora", [
        ("Acerca de Grabadora", "orderFrontStandardAboutPanel:", ""),
        ("-", None, ""),
        ("Grabar rápido  ⌃⌥⌘G", "grabarConAtajo:", "", app_obj),
        ("Mostrar ventana", "mostrarVentana:", "0", app_obj),
        ("-", None, ""),
        ("Ocultar Grabadora", "hide:", "h"),
        ("Salir de Grabadora", "terminate:", "q"),
    ])
    menu("Edición", [
        ("Deshacer", "undo:", "z"),
        ("Rehacer", "redo:", "Z"),
        ("-", None, ""),
        ("Cortar", "cut:", "x"),
        ("Copiar", "copy:", "c"),
        ("Pegar", "paste:", "v"),
        ("Seleccionar todo", "selectAll:", "a"),
    ])
    menu("Ventana", [
        ("Minimizar", "performMiniaturize:", "m"),
        ("Cerrar", "performClose:", "w"),
    ])
    NSApp.setMainMenu_(barra)


def main():
    info = NSBundle.mainBundle().infoDictionary()
    info["CFBundleName"] = "Grabadora"  # que el menú no diga «Python»

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    icono = AQUI / "icono.png"
    if icono.exists():
        app.setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(str(icono)))

    api = motor.Api()
    for x in api._items:
        if x.get("estado") == "transcribiendo":
            api._cola.put(x["id"])

    delegado = App.alloc().initConApi_(api)
    api._app = delegado
    api._emisor = delegado.emitir
    app.setDelegate_(delegado)
    menus(delegado)

    delegado.crearVentana()
    delegado.crearPanel()
    delegado.atajo_global = Atajo(TECLA_G, CTRL | OPT | CMD, delegado.atajo)

    if "--fondo" not in sys.argv:
        delegado.abrir_ventana()
    if "--demo-texto" in sys.argv:  # para revisar la tarjeta con texto sin grabar
        def demo():
            delegado.mostrarPanel(grabar=False)
            delegado.js(delegado.vista_panel, "texto = 'Oye, mañana a las diez tengo junta con el cliente nuevo. Acuérdate de mandarle la propuesta antes y de revisar los anuncios que están pausados.'; $('#duracion').textContent = '0:14'; pintarTexto(); poner('listo');")
        AppHelper.callLater(1.5, demo)
    if "--mostrar" in sys.argv:  # para revisar el cajón sin grabar
        AppHelper.callLater(1.5, lambda: delegado.mostrarPanel(grabar=False))
        if "--esconder" in sys.argv:
            AppHelper.callLater(4, delegado.cerrar_panel)

    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
