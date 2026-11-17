FROM python:3.12-slim

# Install ffmpeg and build dependencies
RUN apt-get update && \
    apt-get install -y \
    ffmpeg \
    gcc \
    g++ \
    python3-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies (dev = pytest, so the tests can run in the container)
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

# Copy application files and tests
COPY *.py pytest.ini ./
COPY tests ./tests

# Run the bot
CMD ["python", "-u", "bot.py"]
