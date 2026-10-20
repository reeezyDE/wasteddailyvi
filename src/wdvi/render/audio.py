"""Audio: Schnitt-Audio + synthetische SFX (keine Lizenzprobleme) + optionale Musik mit Ducking
+ Loudness-Normalisierung (zweistufiges loudnorm auf render.target_lufs, True Peak -1 dB)."""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

import numpy as np

from wdvi.config import Settings
from wdvi.render import _ff
from wdvi.render._ff import RenderError

log = logging.getLogger(__name__)

SR = 48000
SFX_NAMES = ("pop", "whoosh", "boom", "ding", "riser", "record_scratch")
TRUE_PEAK = -1.0
LRA = 11.0
MAX_PREGAIN_DB = 24.0
SILENCE_LUFS = -60.0
# Ankerpunkt je SFX (Sekunden im Sample, die auf den Ereigniszeitpunkt fallen):
# Whoosh mit Spitze auf dem Zoom-Start, Riser läuft auf den Moment hin.
_ANCHOR = {"whoosh": 0.22, "riser": 1.2}
# Lautheitsausgleich je SFX (kurze Klicks wirken sonst zu leise, lange Flächen zu laut)
_BALANCE_DB = {"pop": 4.0, "whoosh": 3.0, "boom": 0.0, "ding": -1.0, "riser": -1.5, "record_scratch": -1.5}


# --------------------------------------------------------------------------- SFX-Synthese


def _swept_band_noise(n: int, sr: int, fc, rel_bw: float, rng: np.random.Generator) -> np.ndarray:
    """Bandpass-Rauschen mit zeitlich veränderlicher Mittenfrequenz fc(t) (FFT-Overlap-Add)."""
    win, hop = 1024, 256
    noise = rng.standard_normal(n + win).astype(np.float32)
    out = np.zeros(n + win, np.float32)
    norm = np.zeros(n + win, np.float32)
    window = np.hanning(win).astype(np.float32)
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    for start in range(0, n, hop):
        c = max(30.0, float(fc(start / sr)))
        bw = max(40.0, c * rel_bw)
        mask = np.exp(-0.5 * ((freqs - c) / bw) ** 2)
        seg = np.fft.irfft(np.fft.rfft(noise[start:start + win] * window) * mask, win).astype(np.float32)
        out[start:start + win] += seg * window
        norm[start:start + win] += window ** 2
    out = out[:n] / np.maximum(norm[:n], 1e-3)
    return out


def _lowpass(sig: np.ndarray, sr: int, cutoff: float) -> np.ndarray:
    """Weicher FFT-Tiefpass (für kurze Signale ausreichend)."""
    n = len(sig)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    gain = 1.0 / np.sqrt(1.0 + (freqs / cutoff) ** 4)  # ~Butterworth 2. Ordnung
    return np.fft.irfft(np.fft.rfft(sig) * gain, n).astype(np.float32)


def _fade(sig: np.ndarray, sr: int, fin: float = 0.002, fout: float = 0.01) -> np.ndarray:
    n_in, n_out = min(len(sig), int(fin * sr)), min(len(sig), int(fout * sr))
    if n_in > 0:
        sig[:n_in] *= np.linspace(0.0, 1.0, n_in, dtype=np.float32)
    if n_out > 0:
        sig[-n_out:] *= np.linspace(1.0, 0.0, n_out, dtype=np.float32)
    return sig


def _chirp(freq: np.ndarray, sr: int) -> np.ndarray:
    return np.sin(2 * np.pi * np.cumsum(freq) / sr)


def synth_sfx(name: str, sr: int = 48000) -> np.ndarray:
    """float32 mono in [-1, 1]: pop, whoosh, boom, ding, riser, record_scratch."""
    return _synth_cached(name, int(sr)).copy()


@lru_cache(maxsize=32)
def _synth_cached(name: str, sr: int) -> np.ndarray:
    if name not in SFX_NAMES:
        raise ValueError(f"Unbekannter Soundeffekt: {name!r} (erlaubt: {', '.join(SFX_NAMES)})")
    rng = np.random.default_rng(SFX_NAMES.index(name) + 7)

    def tt(d: float) -> np.ndarray:
        return np.arange(int(d * sr), dtype=np.float32) / sr

    if name == "pop":  # kurzer, nach unten gleitender Sinus-Blip ("Blasen-Plopp")
        t = tt(0.09)
        f = 420.0 + 900.0 * np.exp(-t * 55.0)
        env = (1.0 - np.exp(-t * 3000.0)) * np.exp(-t * 48.0)
        sig = (_chirp(f, sr) + 0.25 * _chirp(2 * f, sr)) * env
    elif name == "whoosh":  # Bandrausch-Sweep, Spitze nach ~0.22 s
        d = 0.4
        t = tt(d)
        sig = _swept_band_noise(len(t), sr, lambda x: 450.0 + 2600.0 * np.sin(np.pi * min(x / d, 1.0)) ** 1.5,
                                0.45, rng)
        env = np.where(t < 0.22, (t / 0.22) ** 2, np.exp(-(t - 0.22) * 14.0))
        sig = sig * env
    elif name == "boom":  # tiefer Sinus mit Pitch-Drop + Rausch-Transient; Obertöne für Handylautsprecher
        t = tt(0.8)
        f = 38.0 + 22.0 * np.exp(-t * 6.0)
        env = (1.0 - np.exp(-t * 900.0)) * np.exp(-t * 4.2)
        body = _chirp(f, sr) * env
        body = np.tanh(1.8 * body) / np.tanh(1.8)  # Sättigung -> Harmonische (Handylautsprecher)
        body = _lowpass(body, sr, 420.0)  # ... aber kein Brummen/Schnarren in den Höhen
        click = _swept_band_noise(len(t), sr, lambda x: 1800.0 - 1500.0 * min(x / 0.05, 1.0), 0.8, rng)
        click *= np.exp(-t * 60.0) * 0.6
        sig = body + click
    elif name == "ding":  # Glocke: inharmonische Teiltöne um 1.2 kHz
        t = tt(0.6)
        partials = ((1.0, 1.0, 5.0), (2.0, 0.45, 7.5), (2.76, 0.35, 10.0), (4.07, 0.18, 14.0), (5.4, 0.08, 18.0))
        sig = np.zeros_like(t)
        for ratio, amp, dec in partials:
            sig += amp * np.sin(2 * np.pi * 1200.0 * ratio * t) * np.exp(-t * dec)
        sig *= 1.0 - np.exp(-t * 2500.0)
    elif name == "riser":  # ansteigendes Rauschen + Sinus-Sweep, endet abrupt auf dem Moment
        d = 1.2
        t = tt(d)
        noise = _swept_band_noise(len(t), sr, lambda x: 300.0 * (6000.0 / 300.0) ** min(x / d, 1.0), 0.35, rng)
        tone = _chirp(200.0 * (1200.0 / 200.0) ** (t / d), sr)
        env = (t / d) ** 2.2
        sig = (0.75 * noise + 0.45 * tone) * env
        sig = _fade(sig, sr, 0.005, 0.03)
    else:  # record_scratch: tonhöhen-gesweptes Rauschen, zwei "Kratzer"
        d = 0.35
        t = tt(d)

        def fc(x: float) -> float:
            ph = (x / d) * 2.0
            return 2400.0 - 2000.0 * (ph % 1.0) ** 0.6

        noise = _swept_band_noise(len(t), sr, fc, 0.25, rng)
        f = 900.0 - 650.0 * ((t / d * 2.0) % 1.0) ** 0.6
        zip_ = np.sign(np.sin(2 * np.pi * np.cumsum(f) / sr)) * 0.25
        env = np.minimum(1.0, t / 0.01) * (0.6 + 0.4 * np.abs(np.sin(np.pi * t / d * 2.0)))
        env *= np.exp(-np.maximum(0.0, t - 0.25) * 25.0)
        sig = (noise + zip_) * env

    sig = _fade(np.asarray(sig, np.float32), sr, 0.0005 if name in ("pop", "boom", "ding") else 0.003, 0.01)
    peak = float(np.max(np.abs(sig))) if sig.size else 0.0
    if peak > 0:
        sig = sig * (0.9 / peak)
    return sig.astype(np.float32)


# --------------------------------------------------------------------------- Dekodieren / Messen


def _decode(path: Path, info: dict, n_target: int | None = None, loop: bool = False) -> np.ndarray | None:
    """Erste Audiospur -> float32 (n, 2) mit 48 kHz. None, wenn keine Audiospur existiert."""
    if not _ff.has_audio(info):
        return None
    cmd = [_ff.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if loop:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-i", str(path)]
    if n_target is not None:
        cmd += ["-t", f"{n_target / SR + 0.05:.3f}"]
    cmd += ["-map", "0:a:0", "-vn", "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"]
    raw = _ff.run(cmd, timeout=600, what=f"Audio dekodieren ({Path(path).name})").stdout
    n = len(raw) // 8
    return np.frombuffer(raw[: n * 8], dtype="<f4").reshape(-1, 2).astype(np.float32, copy=True)


def _loudnorm_args(target: float) -> str:
    return f"loudnorm=I={target:.1f}:TP={TRUE_PEAK:.1f}:LRA={LRA:.0f}"


def _measure(raw_path: Path, target: float) -> dict | None:
    """loudnorm Pass 1 (print_format=json). None bei Stille / unlesbarer Ausgabe."""
    cmd = [_ff.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "info", "-f", "f32le", "-ar", str(SR), "-ac", "2",
           "-i", str(raw_path), "-af", _loudnorm_args(target) + ":print_format=json", "-f", "null", "-"]
    err = _ff.run(cmd, timeout=600, what="Lautheit messen").stderr.decode("utf-8", "replace")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    if not m:
        log.warning("loudnorm measurement not found in ffmpeg output")
        return None
    try:
        data = json.loads(m.group(0))
        vals = {k: float(data[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (KeyError, ValueError, json.JSONDecodeError):
        return None
    if not np.isfinite(vals["input_i"]) or vals["input_i"] < SILENCE_LUFS:
        return None
    return vals


def _write_raw(path: Path, audio: np.ndarray) -> Path:
    np.ascontiguousarray(audio, dtype="<f4").tofile(path)
    return path


def _db(db: float) -> float:
    return float(10.0 ** (db / 20.0))


# --------------------------------------------------------------------------- Mischen


def mix_sfx(mix: np.ndarray, sfx_events: list[tuple[float, str]], gain_db: float) -> np.ndarray:
    """SFX (mono) in beide Kanäle addieren; Zeiten in Sekunden (Ausgabezeit)."""
    n = len(mix)
    for t, name in sfx_events:
        try:
            sig = synth_sfx(name, SR) * _db(gain_db + _BALANCE_DB.get(name, 0.0))
        except ValueError as e:
            log.warning("%s", e)
            continue
        start = int(round((float(t) - _ANCHOR.get(name, 0.0)) * SR))
        a, b = max(0, start), min(n, start + len(sig))
        if b <= a:
            continue
        mix[a:b] += sig[a - start:b - start, None]
    return mix


def _duck_envelope(program: np.ndarray, depth_db: float = 10.0) -> np.ndarray:
    """Ducking-Faktor (pro Sample) aus der Programm-Hüllkurve: laut -> Musik bis depth_db leiser."""
    hop = SR // 100  # 10 ms
    mono = np.abs(program).mean(axis=1) if program.ndim == 2 else np.abs(program)
    nfr = max(1, int(np.ceil(len(mono) / hop)))
    pad = np.zeros(nfr * hop, np.float32)
    pad[:len(mono)] = mono
    rms = np.sqrt(np.mean(pad.reshape(nfr, hop) ** 2, axis=1) + 1e-12)
    level = 20 * np.log10(rms + 1e-9)
    # -45 dBFS -> 0, -25 dBFS -> volle Absenkung
    amount = np.clip((level + 45.0) / 20.0, 0.0, 1.0)
    env = np.empty_like(amount)
    att, rel = 1 - np.exp(-1 / 2.0), 1 - np.exp(-1 / 35.0)  # ~20 ms Attack, ~350 ms Release
    cur = 0.0
    for i, a in enumerate(amount):
        cur += (a - cur) * (att if a > cur else rel)
        env[i] = cur
    gain = 10 ** (-depth_db * env / 20.0)
    x = (np.arange(len(mono)) / hop).astype(np.float32)
    return np.interp(x, np.arange(nfr), gain).astype(np.float32)


def build_audio(cut_audio: Path, sfx_events: list[tuple[float, str]], settings: Settings, out_wav: Path,
                music_track: Path | None = None) -> Path:
    """cut_audio = Ton des bereits geschnittenen Clips (Ausgabezeit). sfx_events = (Ausgabezeit, Name).
    Ergebnis: 48 kHz Stereo WAV, normalisiert."""
    r = settings.config.render
    target = float(r.target_lufs)
    cut_audio, out_wav = Path(cut_audio), Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    info = _ff.probe(cut_audio)
    dur = _ff.duration(info)

    program = _decode(cut_audio, info)
    n = max(len(program) if program is not None else 0, int(round(dur * SR)))
    if n <= 0:
        raise RenderError(f"Audio: Dauer von {cut_audio.name} ist 0 – nichts zu mischen.")
    prog = np.zeros((n, 2), np.float32)
    if program is not None and len(program):
        prog[:len(program)] = program[:n]
    else:
        log.info("No audio stream in %s, using silence", cut_audio.name)

    raw_path = out_wav.with_name(out_wav.stem + ".mix.f32")
    try:
        # 1) Programm grob auf Ziel-Lautheit bringen, damit SFX/Musik relativ dazu sitzen
        if program is not None and np.any(prog):
            m = _measure(_write_raw(raw_path, prog), target)
            if m is not None:
                pre = float(np.clip(target - m["input_i"], -MAX_PREGAIN_DB, MAX_PREGAIN_DB))
                prog *= _db(pre)
                log.debug("Audio pre-gain %.1f dB (measured %.1f LUFS)", pre, m["input_i"])

        mix = prog.copy()
        # 2) SFX
        if sfx_events:
            mix = mix_sfx(mix, sfx_events, r.sfx_gain_db)
        # 3) Musikbett mit Ducking
        if music_track is not None:
            mix = _add_music(mix, prog, Path(music_track), r.music_gain_db)

        peak = float(np.max(np.abs(mix))) if mix.size else 0.0
        if peak > 4.0:  # extreme Spitzen vorab zähmen (loudnorm regelt den Rest)
            mix *= 4.0 / peak
            peak = 4.0

        # 4) zweistufiges loudnorm -> pcm_s16le 48 kHz Stereo
        _write_raw(raw_path, mix)
        meas = _measure(raw_path, target) if np.any(mix) else None
        if meas is None and peak > 0.99:  # ohne loudnorm: vor der 16-Bit-Wandlung nicht übersteuern
            _write_raw(raw_path, mix * (0.99 / peak))
        cmd = [_ff.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(SR),
               "-ac", "2", "-i", str(raw_path)]
        if meas is not None:
            af = (_loudnorm_args(target) + f":measured_I={meas['input_i']:.2f}:measured_TP={meas['input_tp']:.2f}"
                  f":measured_LRA={meas['input_lra']:.2f}:measured_thresh={meas['input_thresh']:.2f}"
                  f":offset={meas['target_offset']:.2f}:linear=true:print_format=none")
            cmd += ["-af", af]
        cmd += ["-ar", str(SR), "-ac", "2", "-c:a", "pcm_s16le", str(out_wav)]
        _ff.run(cmd, timeout=600, what="Loudness-Normalisierung")
    finally:
        raw_path.unlink(missing_ok=True)
    return out_wav


def _add_music(mix: np.ndarray, program: np.ndarray, track: Path, gain_db: float) -> np.ndarray:
    """Musik geloopt unter das Programm legen (Fade-in/-out, Ducking unter Sprache/Action)."""
    try:
        info = _ff.probe(track)
        music = _decode(track, info, n_target=len(mix), loop=True)
    except RenderError as e:
        log.warning("Music track %s unusable: %s", track.name, e)
        return mix
    if music is None or not len(music):
        log.warning("Music track %s has no audio", track.name)
        return mix
    if len(music) < len(mix):
        reps = int(np.ceil(len(mix) / len(music)))
        music = np.tile(music, (reps, 1))
    music = music[:len(mix)].copy()
    peak = float(np.max(np.abs(music)))
    if peak > 0:
        music *= 0.9 / peak
    fi, fo = min(len(music), SR // 2), min(len(music), SR)
    music[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)[:, None]
    music[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32)[:, None]
    duck = _duck_envelope(program)[:, None]
    return mix + music * duck * _db(gain_db)
