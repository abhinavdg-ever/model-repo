# db-utilities: one-off jobs that load manifests and ground truth into the shared database.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dbutil/ dbutil/
ENTRYPOINT ["python", "-m", "dbutil"]
