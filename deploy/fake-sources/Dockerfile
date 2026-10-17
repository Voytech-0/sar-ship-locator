FROM python:3.12-slim
WORKDIR /app
COPY deploy/fake-sources/app.py /app/app.py
COPY tests/fixtures/scene/meta.json /fixtures/scene/meta.json
COPY tests/fixtures/ais /fixtures/ais
ENV FIXTURES_DIR=/fixtures PORT=8080
EXPOSE 8080
CMD ["python", "/app/app.py"]
