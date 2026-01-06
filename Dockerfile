FROM python:3.11-slim-bookworm

# Install system dependencies
# open-iscsi: for iscsiadm
# zfsutils-linux: for zfs command
RUN apt-get update && apt-get install -y --no-install-recommends \
    open-iscsi \
    zfsutils-linux \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir kubernetes

# Set up working directory
WORKDIR /app

# Copy application code
COPY main.py .

# Make it executable
RUN chmod +x main.py

# ENV Defaults
ENV PYTHONUNBUFFERED=1

# Run the cleaner
CMD ["python3", "main.py"]
