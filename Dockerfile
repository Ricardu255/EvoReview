FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY evoagent ./evoagent
COPY web ./web
COPY skills ./skills
RUN useradd --create-home --uid 10001 evoagent \
    && chown -R evoagent:evoagent /app
USER evoagent
EXPOSE 8080
CMD ["python", "-m", "evoagent"]

