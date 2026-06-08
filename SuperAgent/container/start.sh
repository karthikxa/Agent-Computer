#!/bin/bash
set -e

/dockerstartup/kasm_default_profile.sh &
/dockerstartup/vnc_startup.sh &
sleep 3

DISPLAY=:1 python3 /app/container/desktop_server.py &

mkdir -p /tmp/agent-stream
DISPLAY=:1 ffmpeg -f x11grab -r 30 -s 3840x2160 -i :1 \
  -c:v libx264 -preset ultrafast -tune zerolatency \
  -b:v 8000k -maxrate 8000k -bufsize 16000k \
  -f hls -hls_time 0.5 -hls_list_size 6 -hls_flags delete_segments \
  /tmp/agent-stream/index.m3u8 &
python3 -m http.server 7080 --directory /tmp/agent-stream &

wait
