FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    KUBE_VERSION=$(curl -fsSL https://dl.k8s.io/release/stable.txt) && \
    curl -fLO "https://dl.k8s.io/release/${KUBE_VERSION}/bin/linux/amd64/kubectl" && \
    curl -fLO "https://dl.k8s.io/release/${KUBE_VERSION}/bin/linux/amd64/kubectl.sha256" && \
    echo "$(cat kubectl.sha256)  kubectl" | sha256sum --check && \
    install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && \
    rm kubectl kubectl.sha256 && \
    HELM_VERSION=$(curl -fsSL https://api.github.com/repos/helm/helm/releases/latest | grep '"tag_name"' | sed 's/.*"v\(.*\)".*/\1/') && \
    test -n "$HELM_VERSION" || { echo "ERROR: failed to determine helm version"; exit 1; } && \
    curl -fLO "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" && \
    curl -fLO "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz.sha256sum" && \
    sha256sum --check "helm-v${HELM_VERSION}-linux-amd64.tar.gz.sha256sum" && \
    tar -xzf "helm-v${HELM_VERSION}-linux-amd64.tar.gz" linux-amd64/helm && \
    install -o root -g root -m 0755 linux-amd64/helm /usr/local/bin/helm && \
    rm -rf "helm-v${HELM_VERSION}-linux-amd64.tar.gz" "helm-v${HELM_VERSION}-linux-amd64.tar.gz.sha256sum" linux-amd64 && \
    apt-get purge -y curl && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "PyYAML>=6.0,<7"

WORKDIR /app
COPY pipeline/ pipeline/

ENTRYPOINT ["python", "pipeline/deploy.py"]
