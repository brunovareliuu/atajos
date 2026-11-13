"""Grabadora: native Mac app (AppKit + Liquid Glass) with the Whisper engine in motor.py.

- Main window: history, recording, audio upload.
- Glass panel on the right with ⌃⌥⌘G from any app: it records, types as you talk, and you copy.
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
    NSEvent,
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

# ---------- window styles ----------
TITLED, CLOSABLE, MINI, RESIZABLE = 1 << 0, 1 << 1, 1 << 2, 1 << 3
FULL_CONTENT = 1 << 15
NONACTIVATING = 1 << 7
BORDERLESS = 0
ALL_SPACES, MOVE_TO_ACTIVE_SPACE, FULLSCREEN_AUX = 1 << 0, 1 << 1, 1 << 8
FLOATING_LEVEL = 3
SIZE_ALL = 2 | 16  # flexible width and height

try:
    GlassView = objc.lookUpClass("NSGlassEffectView")
except objc.error:
    GlassView = None


def vidrio(frame, radio, estilo=0):
    """macOS 26 Liquid Glass; on older systems, the classic blur."""
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


# ---------- JS ⇄ Python bridge ----------
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
                    raise AttributeError(f"no such method: {metodo}")
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
    wv.setValue_forKey_(False, "drawsBackground")  # transparent: the glass shows through
    wv.setAutoresizingMask_(SIZE_ALL)
    url = NSURL.fileURLWithPath_(str(UI / html))
    wv.loadFileURL_allowingReadAccessToURL_(url, NSURL.fileURLWithPath_(str(UI)))
    return wv


# ---------- floating panel ----------
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
    """Reports when the mouse enters/exits (even with the app in the background). With al_clic it also takes clicks;
    without it, clicks pass through to whatever is underneath."""

    def updateTrackingAreas(self):
        for t in list(self.trackingAreas()):
            self.removeTrackingArea_(t)
        opciones = 0x01 | 0x80 | 0x200  # entered/exited, always active, visible rect
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
            return None  # no click handler of its own: let whatever is underneath get it
        return v

    def mouseDown_(self, ev):
        fn = getattr(self, "al_clic", None)
        fn and fn()

    def acceptsFirstMouse_(self, ev):
        return True


def vidrio_oscuro(frame, radio, estilo=1, alfa=0.04):
    """Defaults to "clear" with almost no tint (the bar); the card uses regular + more tint so it reads better."""
    v = vidrio(frame, radio, estilo=estilo)
    if GlassView is not None:
        v.setTintColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(0.0, 0.0, 0.0, alfa))
    return v


# ---------- global hotkey (Carbon, no Accessibility permission needed) ----------
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
                print("hotkey:", e)
            return 0

        self._handler = Handler(manejar)  # keep it from being garbage-collected
        objetivo = c.GetApplicationEventTarget()
        tipo = TypeSpec(int.from_bytes(b"keyb", "big"), 5)  # kEventHotKeyPressed
        c.InstallEventHandler(objetivo, self._handler, 1, ctypes.byref(tipo), None, None)
        self._ref = ctypes.c_void_p()
        err = c.RegisterEventHotKey(codigo, modificadores, HotKeyID(int.from_bytes(b"grab", "big"), 1),
                                    objetivo, 0, ctypes.byref(self._ref))
        self.ok = err == 0
        if not self.ok:
            print("Could not register hotkey, error", err)


CMD, OPT, CTRL = 0x100, 0x800, 0x1000
TECLA_G = 5


# ---------- the app ----------
class App(NSObject):
    def initConApi_(self, api):
        self = objc.super(App, self).init()
        self.api = api
        self.vistas = []
        return self

    # main window
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
        # Always open on the desktop (Space) you're on, not the one it was first shown in.
        win.setCollectionBehavior_(MOVE_TO_ACTIVE_SPACE)
        # Tall title bar (like Finder on macOS 26): the traffic lights sit inside the sidebar.
        barra = NSToolbar.alloc().initWithIdentifier_("grabadora")
        barra.setShowsBaselineSeparator_(False)
        win.setToolbar_(barra)
        win.setToolbarStyle_(3)  # unified
        win.setTitlebarSeparatorStyle_(1)  # no line

        fondo = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        fondo.setMaterial_(13)  # hudWindow: the most translucent
        fondo.setBlendingMode_(0)  # behind the window: the desktop shows through
        fondo.setState_(1)
        win.setContentView_(fondo)

        # Liquid Glass sidebar floating inside, as in macOS 26.
        lateral = vidrio(NSMakeRect(8, 8, 268, h - 16), 18)
        lateral.setAutoresizingMask_(16)  # flexible height
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
        win.orderOut_(None)  # close = hide; the hotkey stays alive
        return False

    # ---------- mic pill + dictation card ----------
    # A dark-glass pill on the screen edge with a ring; on hover
    # (or with ⌃⌥⌘G, or a click) a card with the live text slides out beside it.
    BW, BH = 58, 80          # pill
    CW, CH = 384, 470        # card
    MARGEN, HUECO = 8, 12

    @objc.python_method
    def crearPanel(self):
        self.lado = motor.ajuste("lado", "derecha")
        self.abierto = False
        self.fijado = False
        self.sobre = {"burbuja": False, "tarjeta": False}
        self.ancla = None  # (x of the card's right edge, y of its center) sent by Atajos
        self.burbuja = None
        self.CW, self.CH = 330, 60  # starts as a bar; panel.html asks for another size when done

        # card
        p = PanelVidrio.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.CW, self.CH), BORDERLESS | NONACTIVATING, NSBackingStoreBuffered, False)
        flotante(p)
        p.setHasShadow_(False)  # the window shadow drew a rectangular frame; the glass brings its own
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

        # Own pill on the screen edge: off since dictation moved into the Atajos bar (⌃⌥⌘A).
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
        """The screen you're using: the one with the mouse (with two monitors, mainScreen can be the other one)."""
        m = NSEvent.mouseLocation()
        for sc in NSScreen.screens():
            r = sc.frame()
            if r.origin.x <= m.x <= r.origin.x + r.size.width and r.origin.y <= m.y <= r.origin.y + r.size.height:
                return sc
        return NSScreen.mainScreen() or NSScreen.screens()[0]

    @objc.python_method
    def rects(self):
        f, v = self.pantalla().frame(), self.pantalla().visibleFrame()
        if self.ancla:  # next to the Dictation icon in the Atajos bar, on the bar's screen
            x, yc = self.ancla
            for sc in NSScreen.screens():
                r = sc.frame()
                if r.origin.x - 80 <= x <= r.origin.x + r.size.width + 80 and r.origin.y <= yc <= r.origin.y + r.size.height:
                    v = sc.visibleFrame()
                    break
            ch = min(self.CH, v.size.height - 16)
            cy = max(v.origin.y + 8, min(yc - ch / 2, v.origin.y + v.size.height - 8 - ch))
            if getattr(self, "ancla_lado", "derecha") == "izquierda":  # bar on the left edge (⌃⌥⌘Q)
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
        escondida = NSMakeRect(cx + fuera, by, self.CW, ch)  # peeks out from the pill
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

        def fin():  # AppKit expects void
            ventana.invalidateShadow()  # the shadow follows the new shape (otherwise square corners remain)
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
        """Same corners for the glass and the content: the bar is a pill, the card 22 px."""
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
            return  # no pill: the card stays where you drop it
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

    # helpers
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
        p.setMessage_("Choose audio or video files to transcribe")
        if p.runModal() == 1:
            return [str(u.path()) for u in p.URLs()]
        return []

    # lifecycle
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
        ("About Grabadora", "orderFrontStandardAboutPanel:", ""),
        ("-", None, ""),
        ("Quick Record  ⌃⌥⌘G", "grabarConAtajo:", "", app_obj),
        ("Show Window", "mostrarVentana:", "0", app_obj),
        ("-", None, ""),
        ("Hide Grabadora", "hide:", "h"),
        ("Quit Grabadora", "terminate:", "q"),
    ])
    menu("Edit", [
        ("Undo", "undo:", "z"),
        ("Redo", "redo:", "Z"),
        ("-", None, ""),
        ("Cut", "cut:", "x"),
        ("Copy", "copy:", "c"),
        ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a"),
    ])
    menu("Window", [
        ("Minimize", "performMiniaturize:", "m"),
        ("Close", "performClose:", "w"),
    ])
    NSApp.setMainMenu_(barra)


def main():
    info = NSBundle.mainBundle().infoDictionary()
    info["CFBundleName"] = "Grabadora"  # so the menu doesn't say "Python"

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
    if "--demo-texto" in sys.argv:  # for testing: the card with text, without recording
        def demo():
            delegado.mostrarPanel(grabar=False)
            delegado.js(delegado.vista_panel, "texto = 'Hey, tomorrow at ten I have a meeting with the new client. Remember to send them the proposal beforehand and to check the paused ads.'; $('#duracion').textContent = '0:14'; pintarTexto(); poner('listo');")
        AppHelper.callLater(1.5, demo)
    if "--mostrar" in sys.argv:  # for testing: the drawer, without recording
        AppHelper.callLater(1.5, lambda: delegado.mostrarPanel(grabar=False))
        if "--esconder" in sys.argv:
            AppHelper.callLater(4, delegado.cerrar_panel)

    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
