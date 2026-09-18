# vLLM Monitor - Docker Image
# Lightweight, zero-dependency monitoring for local vLLM deployments

FROM python:3.11-slim

# Create non-root user
RUN useradd -m -u 1000 monitor

# Install working directory
WORKDIR /app

# Copy application files
COPY vllm_monitor.py dashboard.html _dash.js config.json /app/
COPY monitor.sh vllm-monitor.service /app/

# Create runtime directories
RUN mkdir -p /app/data && chown -R monitor:monitor /app

# Switch to non-root user
USER monitor

# Expose monitoring port
EXPOSE 8501

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/healthz', timeout=3)" || exit 1

# Run the monitor
CMD ["python", "vllm_monitor.py"]
