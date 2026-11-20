FROM python:3.10-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY static static
COPY seed seed
ENV STATPREDICT_DATA_DIR=/data STATPREDICT_SESSION_URL=""
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8100}"]
