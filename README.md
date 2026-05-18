# ElevenLabs TTS Service

Standalone, self-contained ElevenLabs TTS HTTP service with **per-character
voice routing**, **streaming + non-streaming** output, and **IVC voice
cloning** with persistent caching.

This folder is fully portable: copy it anywhere, run `start.sh`, and you get
a TTS service on port 9885.

## Features

- `POST /tts` accepts `{text, character_name, stream}` and streams audio back.
- One YAML per character under `voice_config/` lets each character either use a
  built-in ElevenLabs voice (`mode: preset`) or be cloned from a local
  reference WAV/MP3 (`mode: clone`).
- Cloned `voice_id`s are persisted in `cache/elevenlabs_voices.json` keyed by
  `voice_cache_key`, so a given reference is only uploaded once.
- Auto-clones eagerly on startup (use `--no-prewarm` for lazy mode).
- `GET /voices`, `GET /characters`, `POST /reload` for introspection.

## Quick start

```bash
cp .env.example .env
# edit .env: ELEVENLABS_API_KEY=sk_...

# Add a character config (see examples in voice_config/)
# Either:
#   - mode: preset → just paste a voice_id from the ElevenLabs dashboard
#   - mode: clone  → drop a reference wav into ref_audio/<character>/ref.wav

bash start.sh
```

`uv` will install dependencies on first run.

## Per-character configuration

Files in `voice_config/<character_name>.yaml`. The file name (without
extension) is the character key looked up by `character_name` in the request.

### `mode: preset` – use an existing ElevenLabs voice_id

```yaml
mode: preset
voice_id: "21m00Tcm4TlvDq8ikWAM"
model_id: eleven_flash_v2_5
voice_settings:
  stability: 0.5
  similarity_boost: 0.75
```

### `mode: clone` – IVC clone from a local audio file

```yaml
mode: clone
reference_audio_path: "ref_audio/小緣/ref.wav"
voice_cache_key: yuan_v1        # MUST be stable; changing it creates a new clone
model_id: eleven_flash_v2_5
voice_settings:
  stability: 0.55
  similarity_boost: 0.85
```

`reference_audio_path` is resolved relative to `ref_audio/`, then to this
service folder, then as an absolute path.

### Optional fields (all configs)

| Field | Default | Notes |
|---|---|---|
| `model_id` | `eleven_flash_v2_5` | `eleven_turbo_v2_5`, `eleven_multilingual_v2`, etc. |
| `output_format` | `mp3_44100_128` | Non-streaming response format. |
| `streaming_output_format` | `pcm_24000` | Streaming response format. |
| `optimize_streaming_latency` | `3` | 0-4 (4 disables text normalisation). |
| `language_code` | (unset) | ISO 639-1; leave unset for auto-detect. |
| `voice_settings.stability` | `0.5` | 0.0-1.0 |
| `voice_settings.similarity_boost` | `0.75` | 0.0-1.0 |
| `voice_settings.style` | `0.0` | 0.0-1.0 |
| `voice_settings.use_speaker_boost` | `true` | |

## HTTP API

### `POST /tts`

```jsonc
{
  "text": "你好，世界！",
  "character_name": "小緣",   // optional, falls back to "default" if missing
  "stream": false              // true → chunked PCM/MP3 stream
}
```

Streaming responses include these headers so the consumer can configure
playback without parsing the body:

```
X-Audio-Format: pcm_24000
X-Sample-Rate: 24000
X-Channels: 1
X-Model-Id: eleven_flash_v2_5
```

### `GET /tts`

Same as POST but takes query parameters (useful for ad-hoc browser testing).

### `GET /characters`

Returns the loaded character configs and their (cached) voice_ids.

### `GET /voices`

Proxies ElevenLabs' `/v2/voices` so you can grep for a `voice_id` you want to
paste into a `mode: preset` config.

### `POST /reload`

Re-scans `voice_config/` without restarting the service.

## Voice cloning notes

- **Creator tier** supports both IVC (Instant Voice Cloning) and PVC.
- This service uses **IVC** because it's automatable. Best results: 1-2 minutes
  of clean, single-speaker audio.
- Each `voice_cache_key` consumes **one slot** of your account's custom-voice
  quota. Pick stable keys (e.g. `yuan_v1`, `mao_v1`) and don't change them
  casually. If you delete a voice via the ElevenLabs dashboard, also remove
  the matching entry from `cache/elevenlabs_voices.json` so it can re-clone.

## CLI flags

`server.py` supports the same options as environment variables:

```
--host                127.0.0.1
--port                9885
--api-key             $ELEVENLABS_API_KEY
--api-base-url        https://api.elevenlabs.io
--voice-config-dir    voice_config
--ref-audio-root      ref_audio
--cache-file          cache/elevenlabs_voices.json
--default-model-id    eleven_flash_v2_5
--default-output-format             mp3_44100_128
--default-streaming-output-format   pcm_24000
--default-optimize-streaming-latency 3
--no-prewarm          # skip eager IVC cloning at startup
--timeout             120.0
```
