
FROM python:3.12-slim
 
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
 
# Standalone yt-dlp binary so `yt-dlp -U` self-updates work
RUN curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
      -o /usr/local/bin/yt-dlp \
 && chmod a+rx /usr/local/bin/yt-dlp
 
RUN pip install --no-cache-dir fastapi "uvicorn[standard]"
 
WORKDIR /app
COPY app/ /app/
RUN chmod +x /app/start.sh
 
VOLUME ["/data"]
EXPOSE 8080
ENV PYTHONUNBUFFERED=1
 
CMD ["/app/start.sh"]
 