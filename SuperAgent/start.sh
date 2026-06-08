#!/usr/bin/env bash
set -euo pipefail

mkdir -p /tmp/agent-stream

if command -v ffmpeg >/dev/null 2>&1; then
  ffmpeg -f x11grab -r 30 -s 3840x2160 -i :1 \
    -c:v libx264 -preset ultrafast -tune zerolatency \
    -b:v 8000k -maxrate 8000k -bufsize 16000k \
    -vf scale=3840:2160 \
    -f hls -hls_time 0.5 -hls_list_size 6 -hls_flags delete_segments \
    /tmp/agent-stream/index.m3u8 >/tmp/agent-stream/ffmpeg.log 2>&1 &
fi

python3 -m http.server 7080 --directory /tmp/agent-stream >/tmp/agent-stream/http.log 2>&1 &

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

tail -f /dev/null
