# Dockerfile for AstralNexus

# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements file
COPY AstralNexus/requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY AstralNexus/ ./AstralNexus

# Set environment variables
ENV ASTRALNEXUS_HOME=/app/AstralNexus

# Expose port for dashboard
EXPOSE 8080

# Default command
CMD ["python", "-m", "AstralNexus.cli.main"]