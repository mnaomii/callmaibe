"""SIP telephony over PJSIP's pjsua2 binding (Linux wheel: TLS, SRTP, ICE, TURN).

Threading: one dedicated "pjsua2" thread creates the Endpoint (threadCnt=0,
mainThreadOnly=True), runs libHandleEvents in a loop and executes a command
queue, so every pjsua2 call and every SIP callback happens on that thread.
Public methods enqueue commands and wait on futures. Audio frames are the one
exception: the conference bridge's clock thread calls our AudioMediaPort,
which only touches Python buffers guarded by locks.

Audio: null sound device, conference bridge at 16 kHz mono 20 ms, one
AudioMediaPort per call connected both ways to the call's audio media.
pjsua2 resamples to and from the codec rate (G.722 is 16 kHz already).
"""

import heapq
import itertools
import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future

import pjsua2 as pj

from .. import __version__, paths
from ..audio import FRAME_BYTES, FRAME_MS, SAMPLE_RATE
from ..config import Config
from .base import Call, CallState, EndReason, Telephony

log = logging.getLogger(__name__)

POLL_MS = 10
IN_QUEUE_FRAMES = 250                      # 5 s of incoming audio before the oldest is dropped
_SILENT_VEC = pj.ByteVector(bytes(FRAME_BYTES))

_TRANSPORTS = {
    "tls": pj.PJSIP_TRANSPORT_TLS,
    "tcp": pj.PJSIP_TRANSPORT_TCP,
    "udp": pj.PJSIP_TRANSPORT_UDP,
}
_PROTO_NAMES = {getattr(pj, n): n.removeprefix("PJMEDIA_TP_PROTO_")
                for n in dir(pj) if n.startswith("PJMEDIA_TP_PROTO_")}


def _audio_format() -> "pj.MediaFormatAudio":
    fmt = pj.MediaFormatAudio()
    fmt.init(pj.PJMEDIA_FORMAT_PCM, SAMPLE_RATE, 1, FRAME_MS * 1000, 16)
    return fmt


def _uri_parts(uri: str) -> tuple[str, str]:
    """'sip:alice@example.org:5061;x=y' -> ('alice', 'example.org:5061')."""
    rest = uri.split(":", 1)[1] if uri.lower().startswith(("sip:", "sips:")) else uri
    rest = rest.split(";", 1)[0].split("?", 1)[0]
    user, _, host = rest.rpartition("@")
    return user, host


def _end_reason(code: int, confirmed: bool) -> EndReason:
    if confirmed:
        return EndReason.REMOTE_HANGUP
    if code in (486, 600):
        return EndReason.BUSY
    if code == 603:  # 403 is left as ERROR: it can be an auth failure, which must not snooze calls
        return EndReason.DECLINED
    if code in (408, 480, 487):
        return EndReason.NO_ANSWER
    return EndReason.ERROR


# --------------------------------------------------------------------------- pjsua2 subclasses
class _Port(pj.AudioMediaPort):
    """Bridges the conference bridge clock thread to Pjsua2Call's Python buffers."""

    def __init__(self, owner: "Pjsua2Call"):
        super().__init__()
        self._owner = owner

    def onFrameRequested(self, frame):
        try:
            data = self._owner._pull()
            frame.buf = pj.ByteVector(data) if data else _SILENT_VEC
            frame.size = FRAME_BYTES
            frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
        except Exception:
            log.exception("onFrameRequested")

    def onFrameReceived(self, frame):
        try:
            if frame.type == pj.PJMEDIA_FRAME_TYPE_AUDIO and frame.size:
                self._owner._push(bytes(frame.buf))
            else:
                self._owner._push(bytes(FRAME_BYTES))
        except Exception:
            log.exception("onFrameReceived")


class _PjCall(pj.Call):
    def __init__(self, acc, owner: "Pjsua2Call | None", call_id: int = pj.PJSUA_INVALID_ID):
        super().__init__(acc, call_id)
        self.owner = owner

    def onCallState(self, prm):
        try:
            if self.owner is not None:
                self.owner._on_state(self.getInfo())
        except Exception:
            log.exception("onCallState")

    def onCallMediaState(self, prm):
        try:
            if self.owner is not None:
                self.owner._on_media(self)
        except Exception:
            log.exception("onCallMediaState")


class _Account(pj.Account):
    def __init__(self, tel: "Pjsua2Telephony"):
        super().__init__()
        self._tel = tel

    def onRegState(self, prm):
        try:
            self._tel._on_reg(prm.code, prm.expiration, prm.reason)
        except Exception:
            log.exception("onRegState")

    def onIncomingCall(self, prm):
        # v1 never takes inbound calls.
        try:
            c = _PjCall(self, None, prm.callId)
            op = pj.CallOpParam()
            op.statusCode = 486
            c.hangup(op)
            log.info("rejected an inbound call")
            self._tel._schedule(1.0, lambda: c)      # keep the object alive briefly, then drop it
        except Exception:
            log.exception("onIncomingCall")


# --------------------------------------------------------------------------- Call
class Pjsua2Call(Call):
    full_duplex = True

    def __init__(self, tel: "Pjsua2Telephony"):
        self._tel = tel
        self._cv = threading.Condition()
        self._state = CallState.DIALING
        self._end: EndReason | None = None
        self.end_detail = ""
        self._confirmed = False
        self._out = bytearray()
        self._out_lock = threading.Lock()
        self._in: deque[bytes] = deque(maxlen=IN_QUEUE_FRAMES)
        self._in_partial = bytearray()
        self._pj: _PjCall | None = None          # touched on the pj thread only
        self._port: _Port | None = None

    # --- public API (any thread) ---
    @property
    def state(self) -> CallState:
        return self._state

    @property
    def end_reason(self) -> EndReason | None:
        return self._end

    def wait_answered(self, timeout_s: float) -> CallState:
        with self._cv:
            done = self._cv.wait_for(
                lambda: self._state in (CallState.ACTIVE, CallState.ENDED), timeout_s)
        if not done:
            log.info("no answer after %.0f s, cancelling", timeout_s)
            self._end_locally(EndReason.NO_ANSWER)
        return self._state

    def play(self, pcm: bytes) -> None:
        if self._state is not CallState.ENDED:
            with self._out_lock:
                self._out += pcm

    def stop_playback(self) -> None:
        with self._out_lock:
            self._out.clear()

    def playing(self) -> bool:
        return bool(self._out)

    def read(self, timeout_s: float) -> bytes | None:
        with self._cv:
            self._cv.wait_for(lambda: self._in or self._state is CallState.ENDED, timeout_s)
            if self._state is CallState.ENDED or not self._in:
                return None
            return self._in.popleft()

    def hangup(self) -> None:
        self._end_locally(EndReason.LOCAL_HANGUP)

    # --- internals ---
    def _end_locally(self, reason: EndReason) -> None:
        with self._cv:
            if self._state is CallState.ENDED:
                return
            self._state = CallState.ENDED
            self._end = reason
            self._cv.notify_all()
        self.stop_playback()
        self._tel._post(self._pj_hangup)

    def _pull(self) -> bytes:                    # clock thread
        with self._out_lock:
            data = bytes(self._out[:FRAME_BYTES])
            del self._out[:FRAME_BYTES]
        if data and len(data) < FRAME_BYTES:
            data += bytes(FRAME_BYTES - len(data))
        return data

    def _push(self, data: bytes) -> None:        # clock thread
        with self._cv:
            if len(data) == FRAME_BYTES and not self._in_partial:
                self._in.append(data)
            else:
                self._in_partial += data
                while len(self._in_partial) >= FRAME_BYTES:
                    self._in.append(bytes(self._in_partial[:FRAME_BYTES]))
                    del self._in_partial[:FRAME_BYTES]
            self._cv.notify_all()

    def _pj_hangup(self) -> None:                # pj thread
        if self._pj is None:
            return
        try:
            self._pj.hangup(pj.CallOpParam())
        except pj.Error as e:
            log.debug("hangup: %s", e.info())

    def _on_state(self, ci) -> None:             # pj thread
        st = ci.state
        log.info("call state: %s (%s %s)", ci.stateText, ci.lastStatusCode, ci.lastReason)
        with self._cv:
            if st in (pj.PJSIP_INV_STATE_EARLY, pj.PJSIP_INV_STATE_CONNECTING):
                if self._state is CallState.DIALING:
                    self._state = CallState.RINGING
            elif st == pj.PJSIP_INV_STATE_CONFIRMED:
                self._confirmed = True
                if self._state is not CallState.ENDED:
                    self._state = CallState.ACTIVE
            elif st == pj.PJSIP_INV_STATE_DISCONNECTED:
                if self._state is not CallState.ENDED:
                    self._state = CallState.ENDED
                    self._end = _end_reason(int(ci.lastStatusCode), self._confirmed)
                    self.end_detail = f"{int(ci.lastStatusCode)} {ci.lastReason}"
                self._tel._post(self._release)
            self._cv.notify_all()

    def _attach_port(self) -> "_Port":            # pj thread
        if self._port is None:
            self._port = _Port(self)
            self._port.createPort(f"callmaibe{id(self) & 0xffff}", _audio_format())
        return self._port

    def _on_media(self, pc: _PjCall) -> None:     # pj thread
        ci = pc.getInfo()
        for i, mi in enumerate(ci.media):
            if mi.type != pj.PJMEDIA_TYPE_AUDIO or mi.status != pj.PJSUA_CALL_MEDIA_ACTIVE:
                continue
            port = self._attach_port()
            aud = pc.getAudioMedia(i)
            aud.startTransmit(port)
            port.startTransmit(aud)
            self._log_media(pc, i)
            self._tel._schedule(3.0, lambda i=i: self._log_media(pc, i, rtp_only=True))

    def _log_media(self, pc: _PjCall, idx: int, rtp_only: bool = False) -> None:   # pj thread
        if self._pj is not pc or self._state is CallState.ENDED:
            return
        try:
            ti = pc.getMedTransportInfo(idx)
            if rtp_only:
                log.info("media: RTP is arriving from %s", ti.srcRtpName or "(nothing yet)")
                return
            si = pc.getStreamInfo(idx)
            log.info("media active: codec %s/%d, transport %s, remote RTP %s, "
                     "local RTP %s, RTP arriving from %s",
                     si.codecName, si.codecClockRate, _PROTO_NAMES.get(si.proto, si.proto),
                     si.remoteRtpAddress, ti.localRtpName, ti.srcRtpName or "(nothing yet)")
            # The dump includes the SRTP crypto suite and the ICE state per media line.
            log.info("call dump:\n%s", pc.dump(True, "  "))
        except pj.Error as e:
            log.warning("media diagnostics failed: %s", e.info())

    def _release(self) -> None:                  # pj thread
        if self._port is not None:
            self._port = None
        if self._pj is not None:
            self._pj.owner = None
            self._pj = None
        self._tel._calls.discard(self)


# --------------------------------------------------------------------------- Telephony
class Pjsua2Telephony(Telephony):
    def __init__(self, cfg: Config, *, port: int = 0):
        self._cfg = cfg
        self._sip_port = port                    # local SIP port; 0 = any
        self._cmds: queue.SimpleQueue = queue.SimpleQueue()
        self._timers: list = []
        self._seq = itertools.count()
        self._thread: threading.Thread | None = None
        self._running = False
        self._ep = None
        self._acc: _Account | None = None
        self._calls: set[Pjsua2Call] = set()
        self._registered = False
        self._reg_event = threading.Event()
        self.last_reg_code: int | None = None
        self.last_reg_reason = ""

    # --- Telephony API ---
    def start(self, reg_wait_s: float = 5.0) -> None:
        """Start the pj thread and register. Waits up to reg_wait_s for the first REGISTER answer."""
        if self._thread is not None:
            return
        ready: Future = Future()
        self._running = True
        self._thread = threading.Thread(target=self._run, args=(ready,), name="pjsua2", daemon=True)
        self._thread.start()
        try:
            ready.result(timeout=30)
        except BaseException:
            self._thread.join(5)
            self._thread = None
            raise
        if self._acc is not None and reg_wait_s > 0:
            self._reg_event.wait(reg_wait_s)

    def stop(self) -> None:
        t = self._thread
        if t is None:
            return
        try:
            self._exec(self._request_stop)
        except Exception:
            log.exception("stop request failed")
        t.join(15)
        if t.is_alive():
            log.error("pjsua2 thread did not stop")
        self._thread = None

    @property
    def registered(self) -> bool:
        return self._registered

    def wait_registration(self, timeout_s: float) -> int | None:
        """Wait for a final REGISTER answer; return its status code (None if none yet)."""
        self._reg_event.wait(timeout_s)
        return self.last_reg_code

    def dial(self, uri: str) -> Pjsua2Call:
        call = Pjsua2Call(self)

        def do():
            if self._acc is None:
                raise RuntimeError("no SIP account configured (account.uri)")
            pc = _PjCall(self._acc, call)
            call._pj = pc
            self._calls.add(call)
            try:
                pc.makeCall(uri, pj.CallOpParam(True))
            except pj.Error:
                call._pj = None
                pc.owner = None
                self._calls.discard(call)
                raise

        self._exec(do)
        log.info("dialing %s", uri)
        return call

    # --- command queue ---
    def _exec(self, fn: Callable, timeout_s: float = 10.0):
        if threading.current_thread() is self._thread:
            return fn()
        if not self._running:
            raise RuntimeError("pjsua2 backend is not running")
        fut: Future = Future()
        self._cmds.put((fn, fut))
        return fut.result(timeout_s)

    def _post(self, fn: Callable) -> None:
        """Run fn on the pj thread's next loop pass (never inline, so it is safe from callbacks)."""
        self._cmds.put((fn, None))

    def _schedule(self, delay_s: float, fn: Callable) -> None:   # pj thread
        heapq.heappush(self._timers, (time.monotonic() + delay_s, next(self._seq), fn))

    def _run_commands(self) -> None:
        while True:
            try:
                fn, fut = self._cmds.get_nowait()
            except queue.Empty:
                break
            if fut is not None and not fut.set_running_or_notify_cancel():
                continue
            try:
                res = fn()
            except pj.Error as e:
                log.debug("pjsua2 command failed: %s", e.info())
                if fut is None:
                    log.warning("pjsua2: %s", e.info())
                else:
                    fut.set_exception(RuntimeError(f"pjsua2: {e.info()}"))
            except BaseException as e:
                if fut is None:
                    log.exception("pjsua2 command failed")
                else:
                    fut.set_exception(e)
            else:
                if fut is not None:
                    fut.set_result(res)
        now = time.monotonic()
        while self._timers and self._timers[0][0] <= now:
            _, _, fn = heapq.heappop(self._timers)
            try:
                fn()
            except Exception:
                log.exception("pjsua2 timer failed")

    # --- the pj thread ---
    def _run(self, ready: Future) -> None:
        try:
            self._init()
        except BaseException as e:  # noqa: BLE001 - handed to the waiting caller
            self._running = False
            self._destroy()
            ready.set_exception(e)
            return
        ready.set_result(None)
        try:
            while self._running:
                self._ep.libHandleEvents(POLL_MS)
                self._run_commands()
        except Exception:
            log.exception("pjsua2 event loop crashed")
        finally:
            self._running = False
            self._shutdown()

    def _request_stop(self) -> None:
        self._running = False

    def _init(self) -> None:
        cfg = self._cfg
        ep = self._ep = pj.Endpoint()
        ep.libCreate()

        epc = pj.EpConfig()
        ua = epc.uaConfig
        ua.threadCnt = 0
        ua.mainThreadOnly = True
        ua.userAgent = f"callmaibe/{__version__}"
        ua.maxCalls = 4
        stun = pj.StringVector()
        for s in cfg.account.stun:
            stun.append(s)
        ua.stunServer = stun
        ua.stunIgnoreFailure = True

        lc = epc.logConfig
        lc.level = 4
        lc.consoleLevel = 0
        lc.msgLogging = True
        lc.filename = str(paths.logs_dir() / "pjsip.log")
        lc.fileFlags = pj.PJ_O_APPEND

        mc = epc.medConfig
        mc.clockRate = SAMPLE_RATE
        mc.sndClockRate = SAMPLE_RATE
        mc.channelCount = 1
        mc.audioFramePtime = FRAME_MS
        mc.noVad = True            # keep RTP flowing (NAT bindings, our own VAD does the work)
        mc.ecTailLen = 0
        ep.libInit(epc)
        ep.audDevManager().setNullDev()

        transport = cfg.account.transport.lower()
        if transport not in _TRANSPORTS:
            raise ValueError(f"account.transport must be tls, tcp or udp, not {transport!r}")
        tcfg = pj.TransportConfig()
        tcfg.port = self._sip_port
        if transport == "tls":
            tcfg.tlsConfig.verifyServer = False
            log.warning("TLS server certificate is NOT verified (no CA bundle configured yet)")
        tid = ep.transportCreate(_TRANSPORTS[transport], tcfg)
        ep.libStart()
        self._set_codecs()
        log.info("pjsua2 %s started (%s transport)", ep.libVersion().full, transport)
        # The password lookup below can leave keyring holding a traceback that reaches this
        # frame. Without the local, a leaked Endpoint can't make the next pj.Endpoint() abort.
        del ep

        if cfg.account.uri:
            self._create_account(tid, transport)
        else:
            log.warning("account.uri is empty: not registering, dialing is disabled")

    def _set_codecs(self) -> None:
        wanted = [c.lower() for c in self._cfg.media.codecs]
        enabled = []
        for ci in self._ep.codecEnum2():
            cid = ci.codecId
            low = cid.lower()
            prio = 0
            for rank, w in enumerate(wanted):
                if low == w or low.startswith(w + "/"):
                    prio = 254 - rank
                    break
            self._ep.codecSetPriority(cid, prio)
            if prio:
                enabled.append(cid)
        if not enabled:
            raise ValueError(f"none of media.codecs {self._cfg.media.codecs} is available")
        log.info("codecs: %s", ", ".join(enabled))

    def _create_account(self, tid: int, transport: str) -> None:
        a = self._cfg.account
        user, host = _uri_parts(a.uri)
        if not host:
            raise ValueError(f"account.uri {a.uri!r} has no domain")
        registrar = f"sip:{host};transport={transport}"

        ac = pj.AccountConfig()
        ac.idUri = a.uri
        ac.regConfig.registrarUri = registrar
        ac.sipConfig.proxies.append(registrar + ";lr")   # INVITEs follow the same TLS route
        ac.sipConfig.transportId = tid
        ac.sipConfig.authCreds.append(
            pj.AuthCredInfo("digest", "*", a.username or user, 0, a.password))
        if not a.password:
            log.warning("no SIP password in $%s", a.password_env)

        nat = ac.natConfig
        nat.iceEnabled = bool(self._cfg.media.ice)
        if a.turn_server:
            nat.turnEnabled = True
            nat.turnServer = a.turn_server
            nat.turnUserName = a.turn_username
            nat.turnPassword = a.turn_password
            nat.turnPasswordType = 0
            log.info("TURN relay %s", a.turn_server)

        med = ac.mediaConfig
        if self._cfg.media.encryption == "srtp":
            med.srtpUse = pj.PJMEDIA_SRTP_MANDATORY
            med.srtpSecureSignaling = 1 if transport == "tls" else 0
        elif self._cfg.media.encryption == "none":
            med.srtpUse = pj.PJMEDIA_SRTP_DISABLED
            med.srtpSecureSignaling = 0
        else:
            raise ValueError(f"media.encryption {self._cfg.media.encryption!r} is not supported "
                             "by pjsua2 (use srtp or none)")

        self._acc = _Account(self)
        self._acc.create(ac)
        log.info("registering %s at %s", a.uri, registrar)

    def _on_reg(self, code: int, expiration: int, reason: str) -> None:   # pj thread
        code = int(code)
        self._registered = code == 200 and expiration > 0
        if code >= 200:
            self.last_reg_code = code
            self.last_reg_reason = reason
            self._reg_event.set()
        log.info("registration: %s %s (expires %s)", code, reason, expiration)

    def _shutdown(self) -> None:                 # pj thread
        try:
            if self._ep is not None and self._calls:
                self._ep.hangupAllCalls()
                end = time.monotonic() + 1.0
                while self._calls and time.monotonic() < end:
                    self._ep.libHandleEvents(POLL_MS)
                    self._run_commands()
            for call in list(self._calls):
                with call._cv:
                    if call._state is not CallState.ENDED:
                        call._state = CallState.ENDED
                        call._end = EndReason.LOCAL_HANGUP
                    call._cv.notify_all()
                call._release()
            self._run_commands()
            self._timers.clear()
            if self._acc is not None:
                try:
                    self._acc.shutdown()
                except pj.Error as e:
                    log.debug("account shutdown: %s", e.info())
                self._acc = None
        except Exception:
            log.exception("pjsua2 shutdown")
        finally:
            self._registered = False
            self._destroy()
            self._fail_pending()

    def _destroy(self) -> None:                  # pj thread
        if self._ep is not None:
            try:
                self._ep.libDestroy()
            except Exception:
                log.exception("libDestroy")
            self._ep = None
        log.info("pjsua2 stopped")

    def _fail_pending(self) -> None:
        while True:
            try:
                _, fut = self._cmds.get_nowait()
            except queue.Empty:
                break
            if fut is not None and fut.set_running_or_notify_cancel():
                fut.set_exception(RuntimeError("pjsua2 backend stopped"))
