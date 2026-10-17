"""pjsua2 backend on Linux/WSL. Run with: pytest -m sip tests/test_pjsua2.py

Needs the pjsua2-python wheel and network access to sip.linphone.org.
"""

import math
import struct
import time

import pytest

pytestmark = pytest.mark.sip
pytest.importorskip("pjsua2")

from callmaibe import paths
from callmaibe.audio import FRAME_BYTES, SAMPLE_RATE
from callmaibe.config import Config
from callmaibe.telephony import create
from callmaibe.telephony.pjsua2 import Pjsua2Call, Pjsua2Telephony


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLMAIBE_HOME", str(tmp_path))
    return tmp_path


def offline_cfg() -> Config:
    cfg = Config()
    cfg.account.uri = ""
    cfg.account.transport = "udp"
    cfg.account.stun = []
    return cfg


def test_start_stop_twice_without_account(home):
    for _ in range(2):                       # the Endpoint singleton must be re-creatable
        tel = create(offline_cfg())
        assert isinstance(tel, Pjsua2Telephony)
        tel.start()
        assert not tel.registered
        with pytest.raises(RuntimeError, match="no SIP account"):
            tel.dial("sip:someone@example.org")
        tel.stop()
        tel.stop()                           # idempotent
    assert (paths.logs_dir() / "pjsip.log").exists()


def test_tls_registration_with_fake_credentials_is_rejected(home, monkeypatch):
    monkeypatch.setenv("CALLMAIBE_SIP_PASSWORD", "definitely-wrong")
    cfg = Config()
    cfg.account.uri = "sip:callmaibe-nonexistent-test-user@sip.linphone.org"
    cfg.account.transport = "tls"
    tel = Pjsua2Telephony(cfg)
    tel.start(reg_wait_s=0)
    try:
        code = tel.wait_registration(30)
        # 4xx from the server proves the TLS handshake worked; pjsip's own
        # transport/timeout failures show up as 408/5xx instead.
        assert code is not None, "no REGISTER answer within 30 s"
        assert 400 <= code < 500 and code != 408, (code, tel.last_reg_reason)
        assert not tel.registered
    finally:
        tel.stop()
    log = (paths.logs_dir() / "pjsip.log").read_text(errors="replace")
    assert "REGISTER sip:sip.linphone.org;transport=tls" in log


def rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    return math.sqrt(sum(s * s for s in struct.unpack(f"<{n}h", pcm[:n * 2])) / max(n, 1))


def sine(seconds: float, hz: float = 440) -> bytes:
    n = int(SAMPLE_RATE * seconds)
    return struct.pack(f"<{n}h", *(int(8000 * math.sin(2 * math.pi * hz * i / SAMPLE_RATE))
                                   for i in range(n)))


@pytest.fixture
def loopback(home):
    """Our backend plus a second, answering account in the same Endpoint, on 127.0.0.1.

    A real SIP call (INVITE, SDP with SRTP-SDES, ICE host candidates, G.722, RTP over
    loopback) without any external account.
    """
    import random

    import pjsua2 as pj

    from callmaibe.telephony.pjsua2 import _PjCall

    port = random.randint(40000, 59000)
    cfg = Config()
    cfg.account.uri = f"sip:callmaibe@127.0.0.1:{port}"
    cfg.account.transport = "udp"
    cfg.account.stun = []
    tel = Pjsua2Telephony(cfg, port=port)
    tel.start(reg_wait_s=0)
    state = {"code": 200, "peers": [], "acc": []}

    class Answerer(pj.Account):
        def onIncomingCall(self, prm):
            w = Pjsua2Call(tel)
            c = _PjCall(self, w, prm.callId)
            w._pj = c
            tel._calls.add(w)
            state["peers"].append(w)
            op = pj.CallOpParam()
            op.statusCode = state["code"]
            c.answer(op)

    def make_peer():
        ac = pj.AccountConfig()
        ac.idUri = f"sip:peer@127.0.0.1:{port}"
        ac.mediaConfig.srtpUse = pj.PJMEDIA_SRTP_MANDATORY
        ac.mediaConfig.srtpSecureSignaling = 0     # plain UDP signalling in this test
        ac.natConfig.iceEnabled = True
        a = Answerer()
        a.create(ac)
        state["acc"].append(a)

    tel._exec(make_peer)
    state["uri"] = f"sip:peer@127.0.0.1:{port}"
    yield tel, state
    tel._exec(lambda: (state["acc"][0].shutdown(), state["acc"].clear()))
    tel.stop()


def test_loopback_call_audio_both_ways_and_remote_hangup(loopback, caplog):
    import logging

    from callmaibe.telephony.base import CallState, EndReason

    caplog.set_level(logging.INFO, logger="callmaibe")
    tel, st = loopback
    call = tel.dial(st["uri"])
    assert call.wait_answered(10) is CallState.ACTIVE
    peer = st["peers"][0]
    assert peer.wait_answered(5) is CallState.ACTIVE    # callee is CONFIRMED once the ACK lands
    tone = sine(1.0)
    call.play(tone)
    peer.play(sine(1.0, 600))
    got_peer, got_call = bytearray(), bytearray()
    end = time.monotonic() + 2.5
    while time.monotonic() < end:
        for src, dst in ((peer, got_peer), (call, got_call)):
            f = src.read(0.02)
            if f:
                dst += f
    assert rms(bytes(got_peer)) > 1000, "caller audio did not reach the peer"
    assert rms(bytes(got_call)) > 1000, "peer audio did not reach the caller"
    assert "media active" in caplog.text and "RTP_SAVP" in caplog.text
    peer.hangup()
    deadline = time.monotonic() + 5
    while call.state is not CallState.ENDED and time.monotonic() < deadline:
        time.sleep(0.05)
    assert call.end_reason is EndReason.REMOTE_HANGUP
    assert call.read(0.1) is None


@pytest.mark.parametrize("code,reason", [(603, "declined"), (486, "busy")])
def test_loopback_rejected_call_maps_end_reason(loopback, code, reason):
    from callmaibe.telephony.base import CallState

    tel, st = loopback
    st["code"] = code
    call = tel.dial(st["uri"])
    assert call.wait_answered(10) is CallState.ENDED
    assert str(call.end_reason) == reason
    assert call.end_detail.startswith(str(code))


def test_loopback_ring_timeout_is_no_answer(loopback):
    from callmaibe.telephony.base import CallState, EndReason

    tel, st = loopback
    st["code"] = 180                      # ring forever
    call = tel.dial(st["uri"])
    assert call.wait_answered(1.0) is CallState.ENDED
    assert call.end_reason is EndReason.NO_ANSWER
    time.sleep(0.5)                       # CANCEL / 487 must not overwrite the reason
    assert call.end_reason is EndReason.NO_ANSWER


def test_audio_media_port_round_trip(home):
    """play() -> our port -> conference bridge -> our other port -> read(), no peer needed."""
    tel = Pjsua2Telephony(offline_cfg())
    tel.start()
    try:
        src, dst = Pjsua2Call(tel), Pjsua2Call(tel)

        def connect():
            src._attach_port().startTransmit(dst._attach_port())

        tel._exec(connect)
        n = SAMPLE_RATE // 2
        tone = struct.pack(f"<{n}h", *(int(8000 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))
                                       for i in range(n)))
        src.play(tone)
        got = bytearray()
        end = time.monotonic() + 2.0
        while time.monotonic() < end and (src.playing() or len(got) < len(tone) + 20 * FRAME_BYTES):
            frame = dst.read(0.1)
            if frame is not None:
                assert len(frame) == FRAME_BYTES
                got += frame
        assert not src.playing()
        start = bytes(got).find(tone[:FRAME_BYTES])
        assert start >= 0 and start % 2 == 0, "tone did not come through the bridge"
        assert bytes(got[start:start + len(tone)]) == tone
    finally:
        tel._exec(lambda: (src._release(), dst._release()))
        tel.stop()
