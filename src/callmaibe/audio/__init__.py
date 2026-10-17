"""Internal audio format, used everywhere between components.

PCM is signed 16-bit little-endian, mono, 16 kHz, passed as `bytes`.
Telephony backends exchange fixed 20 ms frames; TTS may yield chunks of any
length (multiples of 2 bytes). Backends resample at their own edges.
"""

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000   # 320
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH       # 640
