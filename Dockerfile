FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt || true
COPY relay.py .
ENV PYTHONUNBUFFERED=1
EXPOSE 8090
CMD ["python", "relay.py"]
