#!/usr/bin/env python3
"""Standalone ElevenLabs TTS service.

Self-contained: only reads files from this directory.

Endpoints
---------
POST /tts             {text, character_name?, stream}   -> audio bytes
GET  /tts             ?text=...&character_name=...      -> audio bytes (debug)
GET  /health                                             -> service status
GET  /voices                                             -> ElevenLabs account voices
GET  /characters                                         -> loaded voice_config/*.yaml
POST /reload                                             -> reload voice_config without restart

Run
---
    ELEVENLABS_API_KEY=... uv run python server.py --port 9885
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

import httpx
import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from loguru import logger
from pydantic import BaseModel

from voice_manager import VoiceConfig, VoiceManager


SERVICE_ROOT = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Engine                                                                      #
# --------------------------------------------------------------------------- #
class ElevenLabsEngine:
    """Thin wrapper around the ElevenLabs HTTP API.

    We use raw httpx for both streaming and non-streaming so we can pipe
    bytes straight through FastAPI without juggling sync iterators.
    """

    def __init__(
        self,
        api_key: str,
        voice_manager: VoiceManager,
        api_base_url: str = "https://api.elevenlabs.io",
        timeout: float = 120.0,
    ):
        if not api_key:
            raise RuntimeError(
                "ELEVENLABS_API_KEY is required. Set it in .env or pass --api-key."
            )
        self.api_key = api_key
        self.voice_manager = voice_manager
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = timeout

        # Lazy-loaded pykakasi instance for Japanese kanji→hiragana fallback.
        # Only activated when a request specifies Japanese as language_code.
        self._kakasi: Any | None = None
        self._kakasi_failed: bool = False

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clean_text(text: str) -> str:
        """Strip bracketed control tokens (e.g. ``[emotion:happy]``)."""
        return re.sub(r"\[.*?\]", "", text).strip()

    # ------------------------------------------------------------------ #
    # Japanese kanji → hiragana (lazy pykakasi)                          #
    # ------------------------------------------------------------------ #
    _KANJI_RE = re.compile(r"[\u4e00-\u9fff]")

    def _load_kakasi(self) -> bool:
        """Lazy-import pykakasi on first Japanese request. Returns True if usable."""
        if self._kakasi is not None:
            return True
        if self._kakasi_failed:
            return False
        try:
            import pykakasi  # type: ignore

            self._kakasi = pykakasi.kakasi()
            logger.info(
                "pykakasi loaded; Japanese kanji will be converted to hiragana "
                "when language_code is ja."
            )
            return True
        except ImportError:
            self._kakasi_failed = True
            logger.warning(
                "pykakasi not installed; Japanese kanji→hiragana conversion "
                "disabled. Install with: uv add pykakasi"
            )
            return False

    def _kanji_to_hiragana(self, text: str) -> str:
        """Replace kanji-containing tokens with their hiragana reading.

        Preserves katakana, ASCII, digits, and punctuation untouched so the
        model still gets the natural prosody cues of those tokens.
        """
        if not self._load_kakasi():
            return text

        try:
            tokens = self._kakasi.convert(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"pykakasi convert failed, using original text: {exc}")
            return text

        out: list[str] = []
        for item in tokens:
            orig = item.get("orig", "")
            hira = item.get("hira", "")
            if self._KANJI_RE.search(orig) and hira:
                out.append(hira)
            else:
                out.append(orig)
        return "".join(out)

    def _maybe_convert_japanese(
        self,
        text: str,
        cfg: VoiceConfig,
        language_code: Optional[str],
    ) -> str:
        """Apply kanji→hiragana only when the resolved language is Japanese."""
        selected = (language_code or cfg.language_code or "").strip().lower()
        if not selected.startswith("ja"):
            return text
        if not self._KANJI_RE.search(text):
            return text
        converted = self._kanji_to_hiragana(text)
        if converted != text:
            logger.debug(
                f"ja kanji→hiragana: '{text[:40]}...' → '{converted[:40]}...'"
            )
        return converted

    def _headers(self) -> dict[str, str]:
        return {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "*/*",
        }

    def _build_payload(
        self,
        cfg: VoiceConfig,
        text: str,
        language_code: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": text,
            "model_id": cfg.model_id,
            "voice_settings": cfg.voice_settings,
        }
        selected_language = (language_code or cfg.language_code or "").strip()
        if selected_language and selected_language.lower() != "auto":
            payload["language_code"] = selected_language
        return payload

    @staticmethod
    def _file_extension(output_format: str) -> str:
        fmt = output_format.lower()
        if fmt.startswith("mp3"):
            return "mp3"
        if fmt.startswith("pcm"):
            return "pcm"
        if fmt.startswith("ulaw") or fmt.startswith("alaw"):
            return "wav"
        if fmt.startswith("opus"):
            return "ogg"
        if fmt.startswith("flac"):
            return "flac"
        return "bin"

    @staticmethod
    def _media_type(output_format: str) -> str:
        fmt = output_format.lower()
        if fmt.startswith("mp3"):
            return "audio/mpeg"
        if fmt.startswith("pcm"):
            return "application/octet-stream"
        if fmt.startswith("opus"):
            return "audio/ogg"
        if fmt.startswith("flac"):
            return "audio/flac"
        return "application/octet-stream"

    @staticmethod
    def _sample_rate_from_format(output_format: str) -> int:
        match = re.search(r"(\d{4,5})", output_format)
        return int(match.group(1)) if match else 24000

    # ------------------------------------------------------------------ #
    # Non-streaming                                                      #
    # ------------------------------------------------------------------ #
    async def generate_audio(
        self,
        text: str,
        character_name: Optional[str],
        language_code: Optional[str] = None,
    ) -> tuple[bytes, str, str]:
        cfg = self.voice_manager.get_config(character_name)
        voice_id = await asyncio.to_thread(
            self.voice_manager.resolve_voice_id, cfg
        )
        clean_text = self._clean_text(text)
        if not clean_text:
            raise ValueError("text is empty after cleaning")
        clean_text = self._maybe_convert_japanese(clean_text, cfg, language_code)

        url = (
            f"{self.api_base_url}/v1/text-to-speech/{voice_id}"
            f"?output_format={cfg.output_format}"
        )
        payload = self._build_payload(cfg, clean_text, language_code)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, headers=self._headers(), json=payload)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"ElevenLabs TTS failed ({response.status_code}): "
                    f"{response.text[:500]}"
                )
            return (
                response.content,
                self._media_type(cfg.output_format),
                cfg.output_format,
            )

    # ------------------------------------------------------------------ #
    # Streaming                                                          #
    # ------------------------------------------------------------------ #
    async def stream_audio(
        self,
        text: str,
        character_name: Optional[str],
        language_code: Optional[str] = None,
    ) -> AsyncGenerator[bytes, None]:
        cfg = self.voice_manager.get_config(character_name)
        voice_id = await asyncio.to_thread(
            self.voice_manager.resolve_voice_id, cfg
        )
        clean_text = self._clean_text(text)
        if not clean_text:
            raise ValueError("text is empty after cleaning")
        clean_text = self._maybe_convert_japanese(clean_text, cfg, language_code)

        params: dict[str, str] = {
            "output_format": cfg.streaming_output_format,
        }
        if cfg.optimize_streaming_latency is not None:
            params["optimize_streaming_latency"] = str(
                cfg.optimize_streaming_latency
            )

        url = f"{self.api_base_url}/v1/text-to-speech/{voice_id}/stream"
        payload = self._build_payload(cfg, clean_text, language_code)

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                url,
                headers=self._headers(),
                params=params,
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    error_body = await response.aread()
                    raise RuntimeError(
                        f"ElevenLabs streaming failed ({response.status_code}): "
                        f"{error_body.decode('utf-8', 'ignore')[:500]}"
                    )
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    if chunk:
                        yield chunk

    def streaming_audio_meta(self, character_name: Optional[str]) -> dict[str, str]:
        cfg = self.voice_manager.get_config(character_name)
        sample_rate = self._sample_rate_from_format(cfg.streaming_output_format)
        return {
            "X-Audio-Format": cfg.streaming_output_format,
            "X-Sample-Rate": str(sample_rate),
            "X-Channels": "1",
            "X-Model-Id": cfg.model_id,
        }

    # ------------------------------------------------------------------ #
    # Misc                                                               #
    # ------------------------------------------------------------------ #
    async def list_remote_voices(self) -> list[dict[str, Any]]:
        url = f"{self.api_base_url}/v2/voices"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                url,
                headers={"xi-api-key": self.api_key},
                params={"page_size": "100"},
            )
            response.raise_for_status()
            data = response.json()
        voices = data.get("voices") or []
        return [
            {
                "voice_id": v.get("voice_id"),
                "name": v.get("name"),
                "category": v.get("category"),
                "language": v.get("language"),
                "description": v.get("description"),
            }
            for v in voices
        ]


# --------------------------------------------------------------------------- #
# FastAPI app                                                                 #
# --------------------------------------------------------------------------- #
class TTSRequest(BaseModel):
    text: str
    character_name: Optional[str] = None
    language_code: Optional[str] = None
    stream: bool = False


app = FastAPI(title="ElevenLabs TTS Service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine: ElevenLabsEngine | None = None


@app.get("/health")
async def health() -> dict[str, Any]:
    if engine is None:
        return {"ok": False, "error": "engine not initialized"}
    return {
        "ok": True,
        "api_base_url": engine.api_base_url,
        "characters": list(engine.voice_manager.characters.keys()),
        "cache_file": str(engine.voice_manager.cache_file),
    }


@app.get("/characters")
async def characters() -> dict[str, Any]:
    if engine is None:
        raise HTTPException(status_code=503, detail="engine not initialized")
    return {"characters": engine.voice_manager.list_characters()}


@app.get("/voices")
async def voices() -> dict[str, Any]:
    if engine is None:
        raise HTTPException(status_code=503, detail="engine not initialized")
    try:
        return {"voices": await engine.list_remote_voices()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/reload")
async def reload_configs() -> dict[str, Any]:
    if engine is None:
        raise HTTPException(status_code=503, detail="engine not initialized")
    engine.voice_manager.load_configs()
    return {
        "ok": True,
        "characters": list(engine.voice_manager.characters.keys()),
    }


class VoiceConfigUpdate(BaseModel):
    voice_id: Optional[str] = None
    model_id: Optional[str] = None
    output_format: Optional[str] = None
    streaming_output_format: Optional[str] = None
    optimize_streaming_latency: Optional[int] = None
    language_code: Optional[str] = None
    voice_settings: Optional[dict[str, Any]] = None


_CHARACTER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")


def _resolve_yaml_path(character: str) -> Path:
    """Resolve and validate the yaml config path for a character."""
    if not _CHARACTER_NAME_PATTERN.match(character):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid character name. Only alphanumeric, underscore "
                "and dash characters are allowed."
            ),
        )
    if engine is None:
        raise HTTPException(status_code=503, detail="engine not initialized")

    config_dir = engine.voice_manager.config_dir
    yaml_path = (config_dir / f"{character}.yaml").resolve()
    config_dir_resolved = config_dir.resolve()

    try:
        yaml_path.relative_to(config_dir_resolved)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Resolved path escapes voice_config directory.",
        ) from exc

    return yaml_path


@app.get("/voice_config/{character}")
async def get_voice_config(character: str) -> dict[str, Any]:
    """Return the raw yaml config for a character (e.g. 'default')."""
    if engine is None:
        raise HTTPException(status_code=503, detail="engine not initialized")

    yaml_path = _resolve_yaml_path(character)
    if not yaml_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"voice_config/{character}.yaml does not exist",
        )

    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"failed to read yaml: {exc}",
        ) from exc

    return {
        "ok": True,
        "character": character,
        "config": raw,
    }


@app.put("/voice_config/{character}")
async def update_voice_config(
    character: str, update: VoiceConfigUpdate
) -> dict[str, Any]:
    """Update a character's yaml config in-place and reload the voice manager.

    Only fields that are sent in the request body are updated; existing
    fields are preserved. After the file is written, ``load_configs()`` is
    called so the next TTS request picks up the new value immediately.
    """
    if engine is None:
        raise HTTPException(status_code=503, detail="engine not initialized")

    yaml_path = _resolve_yaml_path(character)
    yaml_path.parent.mkdir(parents=True, exist_ok=True)

    if yaml_path.exists():
        try:
            current = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500,
                detail=f"failed to read existing yaml: {exc}",
            ) from exc
    else:
        current = {"mode": "preset"}

    payload = update.model_dump(exclude_none=True)

    if "voice_id" in payload:
        voice_id = (payload["voice_id"] or "").strip()
        if not voice_id:
            raise HTTPException(
                status_code=400,
                detail="voice_id must not be empty",
            )
        current["voice_id"] = voice_id
        # Editing voice_id implies preset mode.
        current.setdefault("mode", "preset")
        if current.get("mode") != "preset":
            current["mode"] = "preset"

    for key in (
        "model_id",
        "output_format",
        "streaming_output_format",
        "optimize_streaming_latency",
        "language_code",
    ):
        if key in payload:
            current[key] = payload[key]

    if "voice_settings" in payload and isinstance(payload["voice_settings"], dict):
        existing_settings = current.get("voice_settings") or {}
        if not isinstance(existing_settings, dict):
            existing_settings = {}
        existing_settings.update(payload["voice_settings"])
        current["voice_settings"] = existing_settings

    tmp = yaml_path.with_suffix(".yaml.tmp")
    try:
        tmp.write_text(
            yaml.safe_dump(
                current,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(yaml_path)
    except Exception as exc:  # noqa: BLE001
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise HTTPException(
            status_code=500,
            detail=f"failed to write yaml: {exc}",
        ) from exc

    engine.voice_manager.load_configs()

    logger.info(
        f"Updated voice_config/{character}.yaml via API "
        f"(voice_id={current.get('voice_id', '')!r})"
    )

    return {
        "ok": True,
        "character": character,
        "config": current,
    }


@app.post("/tts")
async def tts_post(request: TTSRequest):
    return await _handle_tts(request)


@app.get("/tts")
async def tts_get(
    text: str = Query(...),
    character_name: Optional[str] = Query(None),
    language_code: Optional[str] = Query(None),
    stream: bool = Query(False),
):
    return await _handle_tts(
        TTSRequest(
            text=text,
            character_name=character_name,
            language_code=language_code,
            stream=stream,
        )
    )


async def _handle_tts(request: TTSRequest):
    if engine is None:
        raise HTTPException(status_code=503, detail="engine not initialized")
    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="text is required")

    try:
        if request.stream:
            return StreamingResponse(
                engine.stream_audio(
                    request.text,
                    request.character_name,
                    request.language_code,
                ),
                media_type="application/octet-stream",
                headers=engine.streaming_audio_meta(request.character_name),
            )

        audio_bytes, media_type, audio_format = await engine.generate_audio(
            request.text,
            request.character_name,
            request.language_code,
        )
        return Response(
            content=audio_bytes,
            media_type=media_type,
            headers={"X-Audio-Format": audio_format},
        )
    except FileNotFoundError as exc:
        logger.error(f"Reference audio missing: {exc}")
        return JSONResponse(
            status_code=400,
            content={"error": "reference audio missing", "details": str(exc)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"TTS request failed: {exc}")
        return JSONResponse(
            status_code=500,
            content={"error": "TTS request failed", "details": str(exc)},
        )


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #
def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ElevenLabs TTS API Server")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("PORT", "9885"))
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("ELEVENLABS_API_KEY", ""),
        help="ElevenLabs API key (or set ELEVENLABS_API_KEY in .env).",
    )
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("ELEVENLABS_API_BASE_URL", "https://api.elevenlabs.io"),
        help="ElevenLabs API endpoint (regional variants supported).",
    )
    parser.add_argument(
        "--voice-config-dir",
        default=os.getenv(
            "VOICE_CONFIG_DIR", str(SERVICE_ROOT / "voice_config")
        ),
    )
    parser.add_argument(
        "--ref-audio-root",
        default=os.getenv(
            "REF_AUDIO_ROOT", str(SERVICE_ROOT / "ref_audio")
        ),
    )
    parser.add_argument(
        "--cache-file",
        default=os.getenv(
            "CACHE_FILE", str(SERVICE_ROOT / "cache" / "elevenlabs_voices.json")
        ),
    )
    parser.add_argument(
        "--default-model-id",
        default=os.getenv("DEFAULT_MODEL_ID", "eleven_flash_v2_5"),
    )
    parser.add_argument(
        "--default-output-format",
        default=os.getenv("DEFAULT_OUTPUT_FORMAT", "mp3_44100_128"),
    )
    parser.add_argument(
        "--default-streaming-output-format",
        default=os.getenv("DEFAULT_STREAMING_OUTPUT_FORMAT", "pcm_24000"),
    )
    parser.add_argument(
        "--default-optimize-streaming-latency",
        type=int,
        default=int(os.getenv("DEFAULT_OPTIMIZE_STREAMING_LATENCY", "3")),
    )
    parser.add_argument(
        "--no-prewarm",
        action="store_true",
        help="Skip eager IVC cloning at startup (clones lazily on first use).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("HTTP_TIMEOUT", "120.0")),
    )
    return parser


def main() -> None:
    global engine

    load_dotenv(SERVICE_ROOT / ".env", override=False)
    args = _build_arg_parser().parse_args()

    voice_manager = VoiceManager(
        config_dir=Path(args.voice_config_dir),
        ref_audio_root=Path(args.ref_audio_root),
        cache_file=Path(args.cache_file),
        api_key=args.api_key,
        default_model_id=args.default_model_id,
        default_output_format=args.default_output_format,
        default_streaming_output_format=args.default_streaming_output_format,
        default_optimize_streaming_latency=args.default_optimize_streaming_latency,
        api_base_url=args.api_base_url,
    )

    try:
        engine = ElevenLabsEngine(
            api_key=args.api_key,
            voice_manager=voice_manager,
            api_base_url=args.api_base_url,
            timeout=args.timeout,
        )
    except RuntimeError as exc:
        logger.error(str(exc))
        sys.exit(1)

    if not args.no_prewarm:
        voice_manager.prewarm_clones()

    logger.info(
        f"Starting ElevenLabs TTS service on http://{args.host}:{args.port}"
    )
    logger.info(f"  API base URL:     {args.api_base_url}")
    logger.info(f"  voice_config_dir: {args.voice_config_dir}")
    logger.info(f"  ref_audio_root:   {args.ref_audio_root}")
    logger.info(f"  cache_file:       {args.cache_file}")
    logger.info(f"  default model:    {args.default_model_id}")
    logger.info(
        f"  streaming format: {args.default_streaming_output_format} "
        f"(optimize_latency={args.default_optimize_streaming_latency})"
    )
    uvicorn.run(app, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
