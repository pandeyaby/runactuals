FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY examples ./examples
RUN pip install --no-cache-dir . && useradd -r -u 10001 runactuals && mkdir /data && chown runactuals /data
USER runactuals
ENV RUNACTUALS_DB=/data/runactuals.db RUNACTUALS_HOST=0.0.0.0 RUNACTUALS_PORT=4318
EXPOSE 4318 8787
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:4318/healthz')"
ENTRYPOINT ["runactuals"]
CMD ["serve"]
