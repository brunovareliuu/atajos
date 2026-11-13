"""Atajos: a slim vertical dark-glass bar on the right screen edge with icons and progress rings.
Hovering an icon slides its card out to the left (with a pointer aimed at the icon).
⌃⌥⌘A shows / hides it. Icons: Claude (usage), Spotify, Clipboard, Files, AirDrop, Sessions.
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import objc
from AppKit import (
    NSAffineTransform,
    NSImage,
    NSAppearance,
    NSBezierPath,
    NSRunningApplication,
    NSAnimationContext,
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSColor,
    NSEvent,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSNotificationCenter,
    NSOpenPanel,
    NSPanel,
    NSPasteboard,
    NSScreen,
    NSSharingService,
    NSSharingServiceNameSendViaAirDrop,
    NSTrackingArea,
    NSView,
    NSVisualEffectView,
    NSWorkspace,
)
from Foundation import NSURL, NSBundle, NSDistributedNotificationCenter, NSObject, NSTimer
from PyObjCTools import AppHelper
from Quartz import CAMediaTimingFunction, CAShapeLayer
from WebKit import WKUserContentController, WKUserScript, WKWebView, WKWebViewConfiguration

import datos

AQUI = Path(__file__).resolve().parent
UI = AQUI / "ui"

BARRA_W = 58          # bar width
MARGEN = 0            # flush with the screen edge
FILETE = 22           # small curved fillets top and bottom that blend it into the edge
RADIO = 22            # rounded inner corners
MATERIAL = int(os.environ.get("ATAJOS_MATERIAL", "15"))  # NSVisualEffectView: 13 hudWindow, 15 fullScreenUI
TARJETA_W = 340       # width of the card's glass
PICO = 12             # strip to the right of the card that holds the pointer
HUECO = 4             # gap between the pointer tip and the bar
ABAJO_LIBRE = 0.0   # Grabadora's pill is gone: dictation lives in this bar

BORDERLESS, NONACTIVATING = 0, 1 << 7
ALL_SPACES, STATIONARY, FULLSCREEN_AUX = 1 << 0, 1 << 4, 1 << 8
NIVEL = 25
SIZE_ALL = 2 | 16

try:
    GlassView = objc.lookUpClass("NSGlassEffectView")
except objc.error:
    GlassView = None


# (style, black tint): 0 = regular, 1 = clear (more transparent). Try it with ATAJOS_VIDRIO="1,0.2".
VIDRIO = tuple(float(x) for x in os.environ.get("ATAJOS_VIDRIO", "1,0.04").split(","))
VIDRIO = (int(VIDRIO[0]), VIDRIO[1])
TARJETA_VIDRIO = (0, 0.34)  # regular (more blurred) + dark tint


def vidrio_oscuro(frame, radio, tarjeta=False):
    if GlassView is not None:
        v = GlassView.alloc().initWithFrame_(frame)
        v.setCornerRadius_(radio)
        estilo, alfa = VIDRIO
        if tarjeta:  # cards are frostier than the bar so they're readable
            estilo, alfa = TARJETA_VIDRIO
        v.setStyle_(estilo)
        v.setTintColor_(NSColor.colorWithWhite_alpha_(0.0, alfa))
        return v
    v = NSVisualEffectView.alloc().initWithFrame_(frame)
    v.setMaterial_(13)
    v.setBlendingMode_(0)
    v.setState_(1)
    v.setWantsLayer_(True)
    v.layer().setCornerRadius_(radio)
    v.layer().setMasksToBounds_(True)
    return v


# ---------------------------------------------------------------- JS ⇄ Python bridge
PUENTE_JS = """
(() => {
  let n = 0; const pend = {};
  window.__r = (id, ok, val) => { const p = pend[id]; if (!p) return; delete pend[id]; ok ? p[0](val) : p[1](new Error(val)); };
  window.api = new Proxy({}, { get: (_, m) => (...a) => new Promise((res, rej) => {
    const id = ++n; pend[id] = [res, rej];
    window.webkit.messageHandlers.api.postMessage(JSON.stringify({ id, m, a }));
  }) });
})();
"""
EN_HILO_PRINCIPAL = {"fijar", "iconos", "alto_barra", "alto_tarjeta", "clic_icono", "cerrar_tarjeta", "airdrop_elegir",
                     "copiar", "abrir", "revelar", "abrir_airdrop", "elegir_carpeta"}


class Puente(NSObject, protocols=[objc.protocolNamed("WKScriptMessageHandler")]):
    def initWithApi_(self, api):
        self = objc.super(Puente, self).init()
        self.api = api
        return self

    def userContentController_didReceiveScriptMessage_(self, ctrl, msg):
        d = json.loads(str(msg.body()))
        vista = msg.webView()
        id_, metodo, args = d["id"], d["m"], d.get("a", [])

        def correr():
            try:
                if metodo.startswith("_") or not hasattr(self.api, metodo):
                    raise AttributeError(metodo)
                res, ok = getattr(self.api, metodo)(*args), True
            except Exception as e:  # noqa: BLE001
                res, ok = str(e), False
            js = f"window.__r({id_}, {'true' if ok else 'false'}, {json.dumps(res, ensure_ascii=False, default=str)})"
            AppHelper.callAfter(vista.evaluateJavaScript_completionHandler_, js, None)

        if metodo in EN_HILO_PRINCIPAL:
            correr()
        else:
            threading.Thread(target=correr, daemon=True).start()


def airdrop(urls):
    s = NSSharingService.sharingServiceNamed_(NSSharingServiceNameSendViaAirDrop)
    if s is None or not urls:
        return False
    NSApp.activateIgnoringOtherApps_(True)
    if s.canPerformWithItems_(urls):
        s.performWithItems_(urls)
        return True
    return False


class Vista(WKWebView):
    """WebView that accepts the first click and AirDrops whatever you drop on it."""

    def acceptsFirstMouse_(self, ev):
        return True

    def draggingEntered_(self, info):
        self.evaluateJavaScript_completionHandler_("window.soltando && window.soltando(true)", None)
        return 1

    def draggingUpdated_(self, info):
        return 1

    def draggingExited_(self, info):
        self.evaluateJavaScript_completionHandler_("window.soltando && window.soltando(false)", None)

    def prepareForDragOperation_(self, info):
        return True

    def performDragOperation_(self, info):
        self.evaluateJavaScript_completionHandler_("window.soltando && window.soltando(false)", None)
        urls = info.draggingPasteboard().readObjectsForClasses_options_(
            [NSURL], {"NSPasteboardURLReadingFileURLsOnlyKey": True}) or []
        if urls:
            AppHelper.callAfter(airdrop, list(urls))
        return bool(urls)


class PanelBarra(NSPanel):
    def canBecomeKeyWindow(self):
        return False  # the bar never steals keyboard focus

    def canBecomeMainWindow(self):
        return False


class PanelTarjeta(NSPanel):
    def canBecomeKeyWindow(self):
        return True  # so you can type in the clipboard search

    def canBecomeMainWindow(self):
        return False

    def cancelOperation_(self, sender):  # Esc
        AppHelper.callAfter(NSApp.delegate().cerrarTarjeta)


class Rastreador(NSObject):
    """Receives mouse enter/exit/move events even when the app isn't active."""

    def initCon_(self, fn):
        self = objc.super(Rastreador, self).init()
        self.fn = fn
        return self

    def mouseEntered_(self, ev):
        self.fn("entra", ev)

    def mouseExited_(self, ev):
        self.fn("sale", ev)

    def mouseMoved_(self, ev):
        self.fn("mueve", ev)


def rastrear(vista, dueno):
    opciones = 0x01 | 0x02 | 0x80 | 0x200  # entered/exited | moved | activeAlways | inVisibleRect
    vista.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(vista.bounds(), opciones, dueno, None))


def nueva_vista(api, html, frame):
    conf = WKWebViewConfiguration.alloc().init()
    ctrl = WKUserContentController.alloc().init()
    ctrl.addScriptMessageHandler_name_(Puente.alloc().initWithApi_(api), "api")
    ctrl.addUserScript_(WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(PUENTE_JS, 0, True))
    conf.setUserContentController_(ctrl)
    wv = Vista.alloc().initWithFrame_configuration_(frame, conf)
    wv.setValue_forKey_(False, "drawsBackground")
    wv.setAutoresizingMask_(SIZE_ALL)
    wv.loadFileURL_allowingReadAccessToURL_(NSURL.fileURLWithPath_(str(UI / html)), NSURL.fileURLWithPath_(str(UI)))
    return wv


def panel_base(clase, w, h):
    p = clase.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, w, h), BORDERLESS | NONACTIVATING, NSBackingStoreBuffered, False)
    p.setOpaque_(False)
    p.setBackgroundColor_(NSColor.clearColor())
    p.setHasShadow_(True)
    p.setLevel_(NIVEL)
    p.setCollectionBehavior_(ALL_SPACES | STATIONARY | FULLSCREEN_AUX)
    p.setHidesOnDeactivate_(False)
    p.setReleasedWhenClosed_(False)
    return p


def animar(ventana, rect, dur=0.32, despues=None, alfa=None):
    def g(ctx):
        ctx.setDuration_(dur)
        ctx.setTimingFunction_(CAMediaTimingFunction.functionWithControlPoints____(0.2, 1.0, 0.3, 1.0))
        if rect is not None:
            ventana.animator().setFrame_display_(rect, True)
        if alfa is not None:
            ventana.animator().setAlphaValue_(alfa)

    def fin():  # AppKit's block expects void: never return whatever `despues` returns
        if despues:
            despues()

    NSAnimationContext.runAnimationGroup_completionHandler_(g, fin)


# ---------------------------------------------------------------- global hotkey (Carbon, no permissions)
class Atajo:
    """Global hotkey (Carbon). With several hotkeys, each one checks its id and lets the others through."""

    def __init__(self, codigo, mods, fn, ident=1):
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

        c.GetEventParameter.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]

        def manejar(_llamada, evento, _dato):
            hk = HotKeyID()
            c.GetEventParameter(evento, int.from_bytes(b"----", "big"), int.from_bytes(b"hkid", "big"), None,
                                ctypes.sizeof(hk), None, ctypes.byref(hk))
            if hk.id != ident:
                return -9874  # eventNotHandledErr: let the other hotkey handle it
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                print("hotkey:", e)
            return 0

        self._h = Handler(manejar)
        obj = c.GetApplicationEventTarget()
        tipo = TypeSpec(int.from_bytes(b"keyb", "big"), 5)
        c.InstallEventHandler(obj, self._h, 1, ctypes.byref(tipo), None, None)
        self._ref = ctypes.c_void_p()
        err = c.RegisterEventHotKey(codigo, mods, HotKeyID(int.from_bytes(b"atjs", "big"), ident), obj, 0,
                                    ctypes.byref(self._ref))
        self.ok = err == 0
        if not self.ok:
            print("Could not register hotkey", ident, "error", err)


CMD, OPT, CTRL = 0x100, 0x800, 0x1000
TECLA_A = 0
TECLA_Q = 12  # ⌃⌥⌘Q: the bar on the left edge


# ---------------------------------------------------------------- what the views ask for
class Api:
    def __init__(self, app):
        self._app = app
        self._cop = app.copiados

    # bar
    def iconos(self, lista):
        self._app.iconos = lista  # [{tipo, top, alto}] in px from the top of the bar
        return True

    def alto_barra(self, h):
        self._app.altoBarra(float(h))
        return True

    def clic_icono(self, tipo):
        self._app.clicIcono(tipo)
        return True

    def estado(self):
        return self._app.resumen()

    # card
    def alto_tarjeta(self, h):
        self._app.altoTarjeta(float(h))
        return True

    def cerrar_tarjeta(self):
        self._app.cerrarTarjeta()
        return True

    def fijar(self):
        """You clicked or scrolled the card: it no longer closes when the mouse leaves."""
        a = self._app
        if a.tarjeta_tipo and not a.fija:
            a.fija = True
            a.js(a.vista_barra, f"window.fijo && window.fijo({json.dumps(a.tarjeta_tipo)})")
        return True

    def uso(self):
        return {"plan": datos.uso(), "tokens": datos.tokens_locales()}

    def copiados(self):
        return self._cop.lista()

    def copiar(self, i):
        self._cop.copiar(int(i), NSPasteboard.generalPasteboard())
        self._app.empujarEstado()
        return True

    def borrar_copiado(self, i):
        self._cop.borrar(int(i))
        return True

    def spotify(self):
        return datos.spotify()

    def spotify_accion(self, a):
        datos.spotify_accion(a)
        if a == "abrir":
            return {"abierto": True}
        time.sleep(0.15)
        r = datos.spotify()
        self._app.ultimo_spotify = r
        self._app.empujarEstado()
        return r

    def recientes(self):
        return datos.recientes()

    def carpetas(self):
        return datos.carpetas()

    def abrir(self, ruta):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(ruta))
        return True

    def revelar(self, ruta):
        NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(ruta)])
        return True

    def sesiones(self):
        return datos.sesiones()

    def abrir_sesion(self, id_, cwd):
        return bool(datos.abrir_sesion(id_, cwd))

    def carpetas_sesion(self):
        return datos.carpetas_nueva_sesion()

    def ruta_sesion(self, base, nombre):
        r = datos.resolver_carpeta(base, nombre)
        return {"corta": datos._corta(r), "existe": r.is_dir()}

    def nueva_sesion(self, base, nombre):
        try:
            return datos.nueva_sesion(base, nombre)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:120]}

    def elegir_carpeta(self):
        """Browse…: the macOS folder picker (it also lets you create a new one)."""
        self._app.eligiendo = True
        NSApp.activateIgnoringOtherApps_(True)
        p = NSOpenPanel.openPanel()
        p.setCanChooseFiles_(False)
        p.setCanChooseDirectories_(True)
        p.setCanCreateDirectories_(True)
        p.setAllowsMultipleSelection_(False)
        p.setMessage_("Where should the new Claude Code session start?")
        p.setPrompt_("Choose")
        ok = p.runModal() == 1
        self._app.eligiendo = False
        if ok and p.URLs():
            ruta = str(p.URLs()[0].path())
            return {"ruta": ruta, "corta": datos._corta(ruta), "nombre": os.path.basename(ruta) or ruta}
        return None

    def abrir_airdrop(self):
        subprocess.Popen(["open", "/System/Library/CoreServices/Finder.app/Contents/Applications/AirDrop.app"])
        return True

    def airdrop_elegir(self):
        self._app.eligiendo = True
        NSApp.activateIgnoringOtherApps_(True)
        p = NSOpenPanel.openPanel()
        p.setAllowsMultipleSelection_(True)
        p.setCanChooseDirectories_(True)
        p.setMessage_("Choose what to send via AirDrop")
        p.setPrompt_("Send")
        ok = p.runModal() == 1
        self._app.eligiendo = False
        if ok and p.URLs():
            airdrop(list(p.URLs()))
        return ok


def grabadora_corriendo():
    return len(NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.vareliuu.grabadora")) > 0


# ---------------------------------------------------------------- the app
class App(NSObject):
    def init(self):
        self = objc.super(App, self).init()
        self.copiados = datos.Copiados()
        self.api = Api(self)
        self.iconos = []
        self.barra_visible = True
        self.lado = "derecha"  # ⌃⌥⌘A right, ⌃⌥⌘Q left
        self.tarjeta_tipo = None
        self.tarjeta_alto = 300.0
        self.fija = False
        self.eligiendo = False
        self.sobre_barra = False
        self.sobre_tarjeta = False
        self.icono_hover = None
        self.cierre_id = 0
        self.ultimo_spotify = {"abierto": False}
        return self

    # ---------- setup ----------
    @objc.python_method
    def crear(self):
        h = 400
        self.alto_riel = h
        T = h + 2 * FILETE
        b = panel_base(PanelBarra, BARRA_W, T)
        cont = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, BARRA_W, T))
        # Free-form glass (bar + fillets in one piece): an NSVisualEffectView with a mask.
        self.vidrio_barra = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, BARRA_W, T))
        self.vidrio_barra.setMaterial_(MATERIAL)
        self.vidrio_barra.setBlendingMode_(0)
        self.vidrio_barra.setState_(1)
        self.vidrio_barra.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
        self.vidrio_barra.setWantsLayer_(True)
        self.borde_barra = CAShapeLayer.layer()
        self.borde_barra.setFillColor_(None)
        self.borde_barra.setStrokeColor_(NSColor.colorWithWhite_alpha_(1.0, 0.3).CGColor())
        self.borde_barra.setLineWidth_(2.0)  # the mask clips the outer half: 1 px is left inside
        self.vidrio_barra.layer().addSublayer_(self.borde_barra)
        cont.addSubview_(self.vidrio_barra)
        self.vista_barra = nueva_vista(self.api, "barra.html", NSMakeRect(0, FILETE, BARRA_W, h))
        self.vista_barra.setAutoresizingMask_(0)
        cont.addSubview_(self.vista_barra)
        b.setContentView_(cont)
        self.rastro_barra = Rastreador.alloc().initCon_(self.mouseBarra)
        rastrear(cont, self.rastro_barra)
        self.barra, self.cont_barra = b, cont
        self.formaBarra(h)

        tw = TARJETA_W + PICO
        t = panel_base(PanelTarjeta, tw, 300)
        cont = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, tw, 300))
        g = vidrio_oscuro(NSMakeRect(0, 0, TARJETA_W, 300), 22, tarjeta=True)
        g.setAutoresizingMask_(16)  # flexible height; the fixed width leaves the pointer strip free
        self.vidrio_tarjeta = g
        cont.addSubview_(g)
        self.vista_tarjeta = nueva_vista(self.api, "tarjeta.html", NSMakeRect(0, 0, tw, 300))
        cont.addSubview_(self.vista_tarjeta)
        t.setContentView_(cont)
        self.rastro_tarjeta = Rastreador.alloc().initCon_(self.mouseTarjeta)
        rastrear(cont, self.rastro_tarjeta)
        self.tarjeta = t

        self.barra.setFrame_display_(self.marcoBarra(h), True)
        self.barra.orderFrontRegardless()

        # Clipboard every half second; Spotify, usage and counts on a thread every second.
        self.copiados.revisar(NSPasteboard.generalPasteboard())
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.5, self, "tic:", None, True)
        threading.Thread(target=self.bucleEstado, daemon=True).start()

        NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(1 << 1 | 1 << 3, self.clicAfuera)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "pantallasCambiaron:", "NSApplicationDidChangeScreenParametersNotification", None)
        NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "estadoDictado:", "com.vareliuu.dictado.estado", None)
        # Ask Grabadora for its state (in case it's already recording).
        self.avisarDictado("estado")

    # ---------- geometry ----------
    @objc.python_method
    def pantallaDeBarra(self):
        f = self.barra.frame()
        cx, cy = f.origin.x + f.size.width / 2, f.origin.y + f.size.height / 2
        for sc in NSScreen.screens():
            r = sc.frame()
            if r.origin.x <= cx <= r.origin.x + r.size.width and r.origin.y <= cy <= r.origin.y + r.size.height:
                return sc
        return self.pantalla()

    @objc.python_method
    def pantalla(self):
        return NSScreen.mainScreen() or NSScreen.screens()[0]

    @objc.python_method
    def marcoBarra(self, h=None, escondida=False):
        h = (h or self.alto_riel) + 2 * FILETE  # the window includes the fillets
        f, v = self.pantalla().frame(), self.pantalla().visibleFrame()
        base = v.origin.y + v.size.height * ABAJO_LIBRE
        y = base + (v.origin.y + v.size.height - base - h) / 2
        if self.lado == "izquierda":
            x = f.origin.x - BARRA_W - 4 if escondida else f.origin.x + MARGEN
        else:
            x = f.origin.x + f.size.width + 4 if escondida else f.origin.x + f.size.width - BARRA_W - MARGEN
        return NSMakeRect(x, y, BARRA_W, h)

    def pantallasCambiaron_(self, _):
        self.cerrarTarjeta()
        self.barra.setFrame_display_(self.marcoBarra(None, not self.barra_visible), True)

    # ---------- bar ----------
    @objc.python_method
    def altoBarra(self, h):
        if abs(h - self.alto_riel) >= 1:
            self.formaBarra(h)
            self.barra.setFrame_display_(self.marcoBarra(h, not self.barra_visible), True)
            self.barra.invalidateShadow()

    @objc.python_method
    def formaBarra(self, h):
        """Option A silhouette: edge → inward curve → corner → straight down → corner → curve → edge."""
        self.alto_riel = h
        W, F, R, T = float(BARRA_W), float(FILETE), float(RADIO), float(h + 2 * FILETE)
        self.vidrio_barra.setFrame_(NSMakeRect(0, 0, W, T))
        self.vista_barra.setFrame_(NSMakeRect(0, F, W, h))

        def trazo(cerrado):
            bp = NSBezierPath.bezierPath()
            bp.moveToPoint_((W, T))
            bp.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_((W - F, T), F, 0, 270, True)
            bp.lineToPoint_((R, T - F))
            bp.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_((R, T - F - R), R, 90, 180, False)
            bp.lineToPoint_((0, F + R))
            bp.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_((R, F + R), R, 180, 270, False)
            bp.lineToPoint_((W - F, F))
            bp.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_((W - F, 0), F, 90, 0, True)
            if cerrado:
                bp.closePath()
            if self.lado == "izquierda":  # mirror: x → W - x
                t = NSAffineTransform.transform()
                t.translateXBy_yBy_(W, 0)
                t.scaleXBy_yBy_(-1, 1)
                bp.transformUsingAffineTransform_(t)
            return bp

        def pintar(rect):
            NSColor.blackColor().set()
            trazo(True).fill()
            return True
        self.vidrio_barra.setMaskImage_(NSImage.imageWithSize_flipped_drawingHandler_((W, T), False, pintar))
        self.borde_barra.setFrame_(((0, 0), (W, T)))
        self.borde_barra.setPath_(trazo(False).CGPath())

    @objc.python_method
    def iconoEn(self, ev):
        p = self.cont_barra.convertPoint_fromView_(ev.locationInWindow(), None)
        desde_arriba = self.cont_barra.bounds().size.height - p.y - FILETE
        for ic in self.iconos:
            if ic["top"] - 5 <= desde_arriba <= ic["top"] + ic["alto"] + 5:
                return ic["tipo"]
        return None

    @objc.python_method
    def mouseBarra(self, que, ev):
        if que == "sale":
            self.sobre_barra = False
            if self.icono_hover == "dictado":
                self.avisarDictado("salir")
            self.icono_hover = None
            self.js(self.vista_barra, "window.hover && window.hover(null)")
            self.programarCierre()
            return
        self.sobre_barra = True
        self.cierre_id += 1
        tipo = self.iconoEn(ev)
        if tipo != self.icono_hover:
            if self.icono_hover == "dictado":
                self.avisarDictado("salir")
            self.icono_hover = tipo
            self.js(self.vista_barra, f"window.hover && window.hover({json.dumps(tipo)})")
            if tipo == "dictado":
                if not self.fija:
                    self.cerrarTarjeta()
                self.avisarDictado("hover")
            elif tipo and not self.fija:
                self.mostrarTarjeta(tipo)

    @objc.python_method
    def clicIcono(self, tipo):
        if tipo == "dictado":  # Grabadora handles it: here we only tell it where to sit
            self.cerrarTarjeta()
            self.avisarDictado("alternar")
            return
        if self.fija and self.tarjeta_tipo == tipo:
            self.cerrarTarjeta()
            return
        self.mostrarTarjeta(tipo)
        self.fija = True
        self.js(self.vista_barra, f"window.fijo && window.fijo({json.dumps(tipo)})")

    @objc.python_method
    def alternarBarra(self, lado="derecha"):
        if self.barra_visible and lado != self.lado:
            # Open on the other edge: hide it there and bring it in on this one.
            self.cerrarTarjeta()
            self.avisarDictado("cerrar")
            self.barra_visible = False

            def cambiar():
                self.barra.orderOut_(None)
                self.alternarBarra(lado)
            animar(self.barra, self.marcoBarra(None, True), 0.22, cambiar)
            return
        self.barra_visible = not self.barra_visible
        if self.barra_visible:
            if lado != self.lado:
                self.lado = lado
                self.aplicarLado()
            self.barra.setFrame_display_(self.marcoBarra(None, True), False)
            self.barra.orderFrontRegardless()
            animar(self.barra, self.marcoBarra(), 0.42)
        else:
            self.cerrarTarjeta()
            self.avisarDictado("cerrar")

            def fin():
                if not self.barra_visible:
                    self.barra.orderOut_(None)
            animar(self.barra, self.marcoBarra(None, True), 0.3, fin)

    @objc.python_method
    def aplicarLado(self):
        """Mirrors everything for the current edge: the bar's silhouette, the card's glass and pointer."""
        izq = self.lado == "izquierda"
        self.formaBarra(self.alto_riel)
        f = self.vidrio_tarjeta.frame()
        self.vidrio_tarjeta.setFrame_(NSMakeRect(PICO if izq else 0, f.origin.y, TARJETA_W, f.size.height))
        self.js(self.vista_tarjeta, f"document.body.classList.toggle('izq', {'true' if izq else 'false'})")

    # ---------- dictation (lives in Grabadora.app; the two talk through macOS distributed notifications) ----------
    @objc.python_method
    def avisarDictado(self, accion, reintento=False):
        if accion in ("hover", "alternar") and not reintento and not grabadora_corriendo():
            # If Grabadora was quit (⌘Q), open it without a window and resend the notification once it's listening.
            subprocess.Popen(["open", "-g", "-a", "/Applications/Grabadora.app", "--args", "--fondo"])
            AppHelper.callLater(3.0, self.avisarDictado, accion, True)
            return
        info = {"accion": accion}
        if self.barra_visible:
            bf = self.barra.frame()
            izq = self.lado == "izquierda"
            info["lado"] = self.lado
            info["x"] = float(bf.origin.x + bf.size.width + HUECO if izq else bf.origin.x - HUECO)
            info["y"] = float(self.centroIcono("dictado"))
        NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
            "com.vareliuu.dictado", None, info, True)

    def estadoDictado_(self, aviso):
        d = dict(aviso.userInfo() or {})
        dato = {"grabando": bool(d.get("grabando")), "inicio": float(d.get("inicio") or 0)}
        self.js(self.vista_barra, f"window.dictado && window.dictado({json.dumps(dato)})")

    # ---------- card ----------
    @objc.python_method
    def centroIcono(self, tipo):
        bf = self.barra.frame()
        for ic in self.iconos:
            if ic["tipo"] == tipo:
                return bf.origin.y + bf.size.height - FILETE - (ic["top"] + ic["alto"] / 2)
        return bf.origin.y + bf.size.height / 2

    @objc.python_method
    def marcoTarjeta(self, tipo, h):
        v = self.pantallaDeBarra().visibleFrame()  # the bar's screen, not the "active" one (matters with two monitors)
        cy = self.centroIcono(tipo)
        h = min(h, v.size.height - 16)
        y = max(v.origin.y + 8, min(cy - h / 2, v.origin.y + v.size.height - 8 - h))
        bf = self.barra.frame()
        if self.lado == "izquierda":
            x = bf.origin.x + bf.size.width + HUECO
        else:
            x = bf.origin.x - HUECO - TARJETA_W - PICO
        flecha = (y + h) - cy  # px from the top of the card
        return NSMakeRect(x, y, TARJETA_W + PICO, h), flecha

    @objc.python_method
    def mostrarTarjeta(self, tipo):
        self.cierre_id += 1
        self.avisarDictado("salir")  # keep the dictation card from overlapping (unless it's pinned)
        abierta = self.tarjeta_tipo is not None and self.tarjeta.isVisible()
        if abierta and self.tarjeta_tipo == tipo:
            return
        self.tarjeta_tipo = tipo
        rect, flecha = self.marcoTarjeta(tipo, self.tarjeta_alto)
        self.js(self.vista_tarjeta,
                f"window.mostrar && window.mostrar({json.dumps(tipo)}, {flecha:.1f}, {'true' if abierta else 'false'})")
        if abierta:
            animar(self.tarjeta, rect, 0.28)
        else:
            self.tarjeta.setFrame_display_(rect, True)
            self.tarjeta.setAlphaValue_(1.0)
            self.tarjeta.orderFrontRegardless()

    @objc.python_method
    def altoTarjeta(self, h):
        self.tarjeta_alto = h
        if not self.tarjeta_tipo:
            return
        rect, flecha = self.marcoTarjeta(self.tarjeta_tipo, h)
        a = self.tarjeta.frame()
        if abs(a.size.height - rect.size.height) < 1 and abs(a.origin.y - rect.origin.y) < 1:
            return
        self.js(self.vista_tarjeta, f"window.flecha && window.flecha({flecha:.1f})")
        animar(self.tarjeta, rect, 0.26)

    @objc.python_method
    def mouseTarjeta(self, que, ev):
        if que == "sale":
            self.sobre_tarjeta = False
            self.programarCierre()
        else:
            self.sobre_tarjeta = True
            self.cierre_id += 1

    @objc.python_method
    def programarCierre(self):
        if self.fija or self.eligiendo or not self.tarjeta_tipo:
            return
        self.cierre_id += 1
        mi = self.cierre_id

        def quizas():
            if mi == self.cierre_id and not self.sobre_barra and not self.sobre_tarjeta and not self.fija:
                self.cerrarTarjeta()
        AppHelper.callLater(0.3, quizas)

    @objc.python_method
    def cerrarTarjeta(self):
        if not self.tarjeta_tipo:
            return
        self.tarjeta_tipo = None
        self.fija = False
        self.js(self.vista_tarjeta, "window.ocultar && window.ocultar()")
        self.js(self.vista_barra, "window.fijo && window.fijo(null)")
        animar(self.tarjeta, None, 0.14, lambda: (self.tarjeta_tipo is None) and self.tarjeta.orderOut_(None), alfa=0.0)

    @objc.python_method
    def clicAfuera(self, ev):
        if self.fija and not self.eligiendo:
            self.cerrarTarjeta()

    # ---------- state for the bar ----------
    @objc.python_method
    def resumen(self):
        lt = time.localtime()
        hoy = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        return {
            "uso": datos.uso(),
            "tokens": datos.tokens_locales(),
            "spotify": self.ultimo_spotify,
            "copiados_hoy": sum(1 for x in list(self.copiados.items) if x.get("fecha", 0) >= hoy),
        }

    @objc.python_method
    def empujarEstado(self):
        codigo = f"window.estado && window.estado({json.dumps(self.resumen(), ensure_ascii=False)})"
        AppHelper.callAfter(self.js, self.vista_barra, codigo)

    @objc.python_method
    def bucleEstado(self):
        n = 0
        while True:
            try:
                self.ultimo_spotify = datos.spotify() if datos.spotify_abierto() else {"abierto": False}
                if n % 30 == 0:
                    datos.contar_tokens()
                if n % 5 == 0 or self.ultimo_spotify.get("estado") == "playing":
                    self.empujarEstado()
            except Exception as e:  # noqa: BLE001
                print("state:", e)
            n += 1
            time.sleep(1.0)

    def tic_(self, _):
        try:
            if self.copiados.revisar(NSPasteboard.generalPasteboard()):
                self.empujarEstado()
                if self.tarjeta_tipo == "copiados":
                    self.js(self.vista_tarjeta, "window.recargar && window.recargar()")
        except Exception as e:  # noqa: BLE001
            print("clipboard:", e)

    @objc.python_method
    def js(self, vista, codigo):
        vista.evaluateJavaScript_completionHandler_(codigo, None)


def main():
    NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "Atajos"
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon

    menu = NSMenu.alloc().init()  # Edit menu: for ⌘C/⌘V/⌘A in the search field
    raiz = NSMenuItem.alloc().init()
    m = NSMenu.alloc().initWithTitle_("Edit")
    for t, a, k in [("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
                    ("Select All", "selectAll:", "a"), ("Undo", "undo:", "z")]:
        m.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(t, a, k))
    raiz.setSubmenu_(m)
    menu.addItem_(raiz)
    app.setMainMenu_(menu)

    d = App.alloc().init()
    app.setDelegate_(d)
    d.crear()
    d.atajo = Atajo(TECLA_A, CTRL | OPT | CMD, lambda: d.alternarBarra("derecha"), 1)
    d.atajo_izq = Atajo(TECLA_Q, CTRL | OPT | CMD, lambda: d.alternarBarra("izquierda"), 2)
    if "--izquierda" in sys.argv:  # for testing: as if you pressed ⌃⌥⌘Q
        AppHelper.callLater(1.2, d.alternarBarra, "izquierda")
    for arg in sys.argv:
        if arg.startswith("--tarjeta="):  # for testing: opens that card, pinned
            AppHelper.callLater(2.5, d.clicIcono, arg.split("=", 1)[1])
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
