FROM python:3.11-slim

# Install system dependencies including FFmpeg and build tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        gcc \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Set up working directory
WORKDIR /app

# Upgrade pip and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Set default port for web server
ENV PORT=10000

# Run the bot
CMD ["python", "main.py"]
