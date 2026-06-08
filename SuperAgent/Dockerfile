FROM kasmweb/core-ubuntu-focal:1.15.0
USER root

RUN apt-get update && apt-get install -y \
    python3.11 python3-pip \
    ffmpeg \
    tesseract-ocr \
    xdotool xdpyinfo wmctrl scrot xclip \
    chromium-browser \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install \
    flask aiohttp \
    anthropic openai \
    playwright pyotp \
    pytesseract easyocr pillow \
    imaplib2 \
    pyautogui \
    docker \
    sqlalchemy aiosqlite \
    together \
    && playwright install chromium

COPY . /app
WORKDIR /app

EXPOSE 6901 7080 8000

CMD ["/bin/bash", "/app/container/start.sh"]
