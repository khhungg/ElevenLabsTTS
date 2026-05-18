#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-9885}"
DEFAULT_MODEL_ID="${DEFAULT_MODEL_ID:-eleven_flash_v2_5}"
DEFAULT_OUTPUT_FORMAT="${DEFAULT_OUTPUT_FORMAT:-mp3_44100_128}"
DEFAULT_STREAMING_OUTPUT_FORMAT="${DEFAULT_STREAMING_OUTPUT_FORMAT:-pcm_24000}"
DEFAULT_OPTIMIZE_STREAMING_LATENCY="${DEFAULT_OPTIMIZE_STREAMING_LATENCY:-3}"
VOICE_CONFIG_DIR="${VOICE_CONFIG_DIR:-voice_config}"
REF_AUDIO_ROOT="${REF_AUDIO_ROOT:-ref_audio}"
CACHE_FILE="${CACHE_FILE:-cache/elevenlabs_voices.json}"

echo "=== ElevenLabs TTS Service ==="
echo "  Host:                $HOST"
echo "  Port:                $PORT"
echo "  Default model:       $DEFAULT_MODEL_ID"
echo "  Non-stream format:   $DEFAULT_OUTPUT_FORMAT"
echo "  Streaming format:    $DEFAULT_STREAMING_OUTPUT_FORMAT (latency=$DEFAULT_OPTIMIZE_STREAMING_LATENCY)"
echo "  voice_config_dir:    $VOICE_CONFIG_DIR"
echo "  ref_audio_root:      $REF_AUDIO_ROOT"
echo "  cache_file:          $CACHE_FILE"
echo ""

uv run python server.py \
  --host "$HOST" \
  --port "$PORT" \
  --default-model-id "$DEFAULT_MODEL_ID" \
  --default-output-format "$DEFAULT_OUTPUT_FORMAT" \
  --default-streaming-output-format "$DEFAULT_STREAMING_OUTPUT_FORMAT" \
  --default-optimize-streaming-latency "$DEFAULT_OPTIMIZE_STREAMING_LATENCY" \
  --voice-config-dir "$VOICE_CONFIG_DIR" \
  --ref-audio-root "$REF_AUDIO_ROOT" \
  --cache-file "$CACHE_FILE" \
  "$@"
