FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static ./static
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8080", "--threads=4", "app:app"]
