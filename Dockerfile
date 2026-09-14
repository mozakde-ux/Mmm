FROM python:3.11-slim

# Install system dependencies (ffmpeg), curl, unzip + fonts for tweet-card rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    fontconfig \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# Persian font (Vazirmatn) for correct fa/ar tweet-card rendering.
# Code falls back to Noto/DejaVu if the download ever fails.
RUN mkdir -p /usr/share/fonts/truetype/vazirmatn \
    && curl -fsSL -o /usr/share/fonts/truetype/vazirmatn/Vazirmatn.ttf \
        "https://github.com/google/fonts/raw/main/ofl/vazirmatn/Vazirmatn%5Bwght%5D.ttf" \
    && fc-cache -f > /dev/null || true

# Install Deno securely and link it globally into the system PATH
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL="/root/.deno"
ENV PATH="$DENO_INSTALL/bin:$PATH"

WORKDIR /app

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Force update yt-dlp to ensure signature logic is up to date
RUN pip install --no-cache-dir --upgrade yt-dlp

# Copy everything else (including config.json, cookies.txt, etc.)
COPY . .

# Start the bot
CMD ["python", "bot.py"]
