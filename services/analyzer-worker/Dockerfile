FROM python:3.12-slim

# Apply the distribution's security updates at build time. The upstream image
# lags Debian's security archive by days to weeks; Trivy in CI fails the build
# on any fixable CRITICAL/HIGH, and this is what makes it pass.
RUN apt-get update     && apt-get upgrade -y --no-install-recommends     && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --no-create-home --uid 10001 sentinel
USER 10001

CMD ["python", "-m", "app.worker"]
