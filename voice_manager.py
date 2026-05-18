"""Per-character voice configuration manager for ElevenLabs TTS service.

Loads `voice_config/*.yaml` files (one per character) and resolves the right
ElevenLabs voice_id for a given `character_name`.  Supports two modes:

* ``mode: preset`` – use an existing ElevenLabs voice_id (built-in or already
  cloned via the dashboard).
* ``mode: clone``  – instant voice clone (IVC) from a local reference WAV/MP3.
  Cloned voice IDs are persisted in ``cache/elevenlabs_voices.json`` keyed by
  ``voice_cache_key`` so we never re-upload the same file.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml
from loguru import logger


@dataclass
class VoiceConfig:
    """Resolved voice configuration for a single character."""

    character: str
    mode: str  # "preset" or "clone"
    voice_id: str = ""
    reference_audio_path: str = ""
    voice_cache_key: str = ""
    model_id: str = "eleven_flash_v2_5"
    output_format: str = "mp3_44100_128"
    streaming_output_format: str = "pcm_24000"
    optimize_streaming_latency: int = 3
    language_code: Optional[str] = None
    voice_settings: dict[str, Any] = field(
        default_factory=lambda: {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        }
    )


class VoiceManager:
    """Load voice configs and resolve cloned voice IDs lazily/eagerly."""

    def __init__(
        self,
        config_dir: Path,
        ref_audio_root: Path,
        cache_file: Path,
        api_key: str,
        default_model_id: str = "eleven_flash_v2_5",
        default_output_format: str = "mp3_44100_128",
        default_streaming_output_format: str = "pcm_24000",
        default_optimize_streaming_latency: int = 3,
        api_base_url: str = "https://api.elevenlabs.io",
    ):
        self.config_dir = Path(config_dir)
        self.ref_audio_root = Path(ref_audio_root)
        self.cache_file = Path(cache_file)
        self.api_key = api_key
        self.default_model_id = default_model_id
        self.default_output_format = default_output_format
        self.default_streaming_output_format = default_streaming_output_format
        self.default_optimize_streaming_latency = default_optimize_streaming_latency
        self.api_base_url = api_base_url.rstrip("/")

        self.characters: dict[str, VoiceConfig] = {}
        self._cache_lock = threading.Lock()
        self._clone_locks: dict[str, threading.Lock] = {}

        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.load_configs()

    # ------------------------------------------------------------------ #
    # Config loading                                                     #
    # ------------------------------------------------------------------ #
    def load_configs(self) -> None:
        """Scan ``voice_config/`` and populate ``self.characters``."""
        self.characters.clear()

        if not self.config_dir.is_dir():
            logger.warning(
                f"voice_config dir does not exist: {self.config_dir}. "
                "Service will only accept built-in voice_id fallback."
            )
            return

        yaml_files = sorted(
            [p for p in self.config_dir.iterdir() if p.suffix in {".yaml", ".yml"}]
        )
        if not yaml_files:
            logger.warning(f"No yaml files in {self.config_dir}.")
            return

        for yaml_path in yaml_files:
            character = yaml_path.stem
            try:
                cfg = self._parse_config_file(yaml_path, character)
                self.characters[character] = cfg
                logger.info(
                    f"Loaded voice config for '{character}' "
                    f"(mode={cfg.mode}, model={cfg.model_id})"
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Failed to load {yaml_path}: {exc}")

        logger.info(
            f"VoiceManager: {len(self.characters)} characters loaded "
            f"({', '.join(self.characters.keys()) or 'none'})"
        )

    def _parse_config_file(self, yaml_path: Path, character: str) -> VoiceConfig:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        mode = str(raw.get("mode", "preset")).lower().strip()
        if mode not in {"preset", "clone"}:
            raise ValueError(f"Invalid mode '{mode}' in {yaml_path.name}")

        cfg = VoiceConfig(
            character=character,
            mode=mode,
            voice_id=str(raw.get("voice_id", "") or ""),
            reference_audio_path=str(raw.get("reference_audio_path", "") or ""),
            voice_cache_key=str(
                raw.get("voice_cache_key", "") or character
            ),
            model_id=str(raw.get("model_id", "") or self.default_model_id),
            output_format=str(
                raw.get("output_format", "") or self.default_output_format
            ),
            streaming_output_format=str(
                raw.get("streaming_output_format", "")
                or self.default_streaming_output_format
            ),
            optimize_streaming_latency=int(
                raw.get(
                    "optimize_streaming_latency",
                    self.default_optimize_streaming_latency,
                )
            ),
            language_code=raw.get("language_code") or None,
        )
        if raw.get("voice_settings"):
            cfg.voice_settings.update(raw["voice_settings"])

        if mode == "preset" and not cfg.voice_id:
            raise ValueError(
                f"{yaml_path.name}: mode=preset requires voice_id"
            )
        if mode == "clone" and not cfg.reference_audio_path:
            raise ValueError(
                f"{yaml_path.name}: mode=clone requires reference_audio_path"
            )
        return cfg

    # ------------------------------------------------------------------ #
    # Voice resolution                                                   #
    # ------------------------------------------------------------------ #
    def get_config(self, character_name: Optional[str]) -> VoiceConfig:
        """Return the VoiceConfig for ``character_name``; fall back to default."""
        if character_name and character_name in self.characters:
            return self.characters[character_name]
        if "default" in self.characters:
            if character_name:
                logger.debug(
                    f"Character '{character_name}' not found, using 'default'"
                )
            return self.characters["default"]
        if self.characters:
            first = next(iter(self.characters))
            logger.warning(
                f"No 'default' character; falling back to first config '{first}'"
            )
            return self.characters[first]
        raise RuntimeError(
            "No voice configs loaded; cannot resolve voice for "
            f"character_name={character_name!r}"
        )

    def resolve_voice_id(self, cfg: VoiceConfig) -> str:
        """Return a usable voice_id, performing IVC clone if necessary."""
        if cfg.mode == "preset":
            return cfg.voice_id

        cached = self._get_cached_voice_id(cfg.voice_cache_key)
        if cached:
            return cached

        lock = self._clone_locks.setdefault(cfg.voice_cache_key, threading.Lock())
        with lock:
            cached = self._get_cached_voice_id(cfg.voice_cache_key)
            if cached:
                return cached
            return self._clone_voice(cfg)

    # ------------------------------------------------------------------ #
    # IVC cloning                                                        #
    # ------------------------------------------------------------------ #
    def _resolve_ref_audio_path(self, cfg: VoiceConfig) -> Path:
        """Resolve ``reference_audio_path`` against a few sensible roots.

        Rules (first hit wins):
          1. Absolute path -> use as-is.
          2. Path starts with ``ref_audio/`` -> resolve relative to the
             service root (parent of ``ref_audio_root``).
          3. Otherwise -> resolve relative to ``ref_audio_root`` first, then
             to the service root as a fallback.
        """
        raw = cfg.reference_audio_path
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            if not candidate.exists():
                raise FileNotFoundError(
                    f"Reference audio not found for character "
                    f"'{cfg.character}': {candidate}"
                )
            return candidate

        service_root = self.ref_audio_root.parent
        tried: list[Path] = []

        norm = raw.replace("\\", "/").lstrip("/")
        if norm.startswith("ref_audio/"):
            primary = (service_root / norm).resolve()
            tried.append(primary)
            if primary.exists():
                return primary
            inner = (
                self.ref_audio_root / norm[len("ref_audio/") :]
            ).resolve()
            tried.append(inner)
            if inner.exists():
                return inner
        else:
            primary = (self.ref_audio_root / norm).resolve()
            tried.append(primary)
            if primary.exists():
                return primary
            fallback = (service_root / norm).resolve()
            tried.append(fallback)
            if fallback.exists():
                return fallback

        tried_str = "\n  - " + "\n  - ".join(str(p) for p in tried)
        raise FileNotFoundError(
            f"Reference audio not found for character '{cfg.character}'. "
            f"Configured path: {raw!r}. Tried:{tried_str}"
        )

    def _clone_voice(self, cfg: VoiceConfig) -> str:
        try:
            from elevenlabs import ElevenLabs
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "elevenlabs python package is required for voice cloning. "
                "Install it via `uv add elevenlabs` (or pip)."
            ) from exc

        ref_path = self._resolve_ref_audio_path(cfg)
        clone_name = cfg.voice_cache_key or cfg.character
        logger.info(
            f"Cloning ElevenLabs voice for '{cfg.character}' "
            f"(cache_key='{cfg.voice_cache_key}', ref='{ref_path}')..."
        )

        client = ElevenLabs(api_key=self.api_key)
        with ref_path.open("rb") as f:
            voice = client.voices.ivc.create(
                name=clone_name,
                description=f"Auto-cloned by ElevenLabsTTS for {cfg.character}",
                files=[f],
            )
        voice_id = getattr(voice, "voice_id", None) or getattr(voice, "id", None)
        if not voice_id:
            raise RuntimeError(
                f"ElevenLabs IVC did not return a voice_id: {voice!r}"
            )

        self._set_cached_voice_id(
            cfg.voice_cache_key,
            voice_id=str(voice_id),
            name=clone_name,
            reference_audio_path=str(ref_path),
        )
        logger.info(
            f"Cloned voice for '{cfg.character}' -> voice_id={voice_id} "
            f"(cache_key='{cfg.voice_cache_key}')"
        )
        return str(voice_id)

    def prewarm_clones(self) -> None:
        """Eagerly resolve all `mode=clone` characters at startup."""
        for character, cfg in self.characters.items():
            if cfg.mode != "clone":
                continue
            try:
                voice_id = self.resolve_voice_id(cfg)
                logger.info(
                    f"Prewarm: '{character}' ready (voice_id={voice_id})"
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Prewarm failed for '{character}': {exc}")

    # ------------------------------------------------------------------ #
    # Voice-id cache (cache/elevenlabs_voices.json)                      #
    # ------------------------------------------------------------------ #
    def _load_cache(self) -> dict[str, Any]:
        if not self.cache_file.exists():
            return {}
        try:
            return json.loads(self.cache_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not read {self.cache_file}: {exc}")
            return {}

    def _save_cache(self, cache: dict[str, Any]) -> None:
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.cache_file)

    def _get_cached_voice_id(self, cache_key: str) -> Optional[str]:
        if not cache_key:
            return None
        with self._cache_lock:
            cache = self._load_cache()
            entry = cache.get(cache_key)
            if isinstance(entry, dict):
                return entry.get("voice_id")
            if isinstance(entry, str):
                return entry
            return None

    def _set_cached_voice_id(
        self,
        cache_key: str,
        voice_id: str,
        name: str,
        reference_audio_path: str,
    ) -> None:
        with self._cache_lock:
            cache = self._load_cache()
            cache[cache_key] = {
                "voice_id": voice_id,
                "name": name,
                "reference_audio_path": reference_audio_path,
                "created_at": datetime.utcnow().isoformat() + "Z",
            }
            self._save_cache(cache)

    # ------------------------------------------------------------------ #
    # Introspection                                                      #
    # ------------------------------------------------------------------ #
    def list_characters(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for character, cfg in self.characters.items():
            voice_id = cfg.voice_id
            if cfg.mode == "clone":
                voice_id = self._get_cached_voice_id(cfg.voice_cache_key) or ""
            out.append(
                {
                    "character": character,
                    "mode": cfg.mode,
                    "voice_id": voice_id,
                    "voice_cache_key": cfg.voice_cache_key,
                    "reference_audio_path": cfg.reference_audio_path,
                    "model_id": cfg.model_id,
                }
            )
        return out
