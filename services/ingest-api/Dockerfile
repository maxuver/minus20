FROM python:3.12-slim

# Apply the distribution's security updates at build time. The upstream image
# lags Debian's security archive by days to weeks; Trivy in CI fails the build
# on any fixable CRITICAL/HIGH, and this is what makes it pass.
RUN apt-get update     && apt-get upgrade -y --no-install-recommends     && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --no-create-home --uid 10001 minus20
USER 10001

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
