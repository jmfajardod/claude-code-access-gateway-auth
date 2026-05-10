# Merged Dockerfile: azureagent_infra base + data-reports-mcp-server install.
# This builds the full prod base image inline (instead of FROM-ing a pre-built
# company ECR image) so the POC environment can validate that the prod
# Dockerfile actually works end-to-end.
FROM --platform=linux/amd64 python:3.13.12-slim

ARG zoneinfo=/usr/share/zoneinfo/America/Bogota

# Arguments for the versions
ARG AWS_CLI_VERSION=2.25.5
ARG CDK_VERSION=2.1031.2
ARG INSPECTOR_SBOMGEN=1.12.1
ARG DOCKER_VERSION=5:28.5.2-1~debian.12~bookworm
ARG GNUREADLINE_VERSION=8.2.13
ARG INVOKE_VERSION=2.2.0
ARG NODE_MAJOR=20
ARG GIT_VERSION=2.53.0
ARG NPM_VERSION=10.8.2
ARG UV_VERSION=0.7.19
ARG PIP_VERSION=25.1.1
ARG PIPENV_VERSION=2025.0.4
ARG SETUPTOOLS_VERSION=80.9.0
ARG WHEEL_VERSION=0.45.1

ENV DEBIAN_FRONTEND=noninteractive
ENV JRE_CKSUM="230d97a6b16a0735f15013a91f7582a22282ec12bdfaec291ab63274cc075efb  OpenJDK15U-jre_x64_linux_hotspot_15_36.tar.gz"
ENV JRE_TAR="OpenJDK15U-jre_x64_linux_hotspot_15_36.tar.gz"
ENV JRE_URL="https://github.com/AdoptOpenJDK/openjdk15-binaries/releases/download/jdk-15%2B36/${JRE_TAR}"
ENV MYPYLS_URL="https://github.com/matangover/mypyls/archive/master.zip#egg=mypyls[default-mypy]"
ENV PIP_NO_CACHE_DIR="false"

RUN echo "America/Bogota" | tee /etc/timezone

# Install common tools
RUN ln -sf ${zoneinfo} /etc/localtime \
    && echo "APT::Get::Assume-Yes \"true\";" > /etc/apt/apt.conf.d/90assumeyes \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends \
    apt-utils \
    build-essential \
    ca-certificates \
    curl \
    gcc \
    gcc-multilib \
    gettext \
    git \
    git \
    gnupg \
    gpg \
    gpgconf \
    groff \
    iputils-ping \
    jq \
    less \
    libcurl4 \
    libcurl4-gnutls-dev  \
    libexpat1-dev  \
    libicu76 \
    libssl-dev  \
    libunwind8 \
    libz-dev  \
    make \
    netcat-openbsd \
    python3-dev \
    ruby-full \
    ssh \
    sudo \
    unzip \
    xz-utils \
    zip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js and NPM
RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install npm@${NPM_VERSION} -g \
    && rm -rf /var/lib/apt/lists/*


# Download x64 static build of wkhtmltox
RUN apt-get update && apt-get install -y curl rpm2cpio cpio \
    && curl -L https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox-0.12.6.1-3.almalinux9.x86_64.rpm -o /tmp/wkhtmltox.rpm \
    && cd /tmp \
    && rpm2cpio wkhtmltox.rpm | cpio -idmv \
    && mkdir -p /opt/wkhtmltox/bin/ \
    && mv /tmp/usr/local/bin/wkhtmltopdf /opt/wkhtmltox/bin/wkhtmltopdf \
    && chmod +x /opt/wkhtmltox/bin/wkhtmltopdf \
    && rm -rf /tmp/wkhtmltox.rpm /tmp/usr \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install AWS CLI
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64-$AWS_CLI_VERSION.zip" -o "awscliv2.zip" \
    && unzip -o awscliv2.zip \
    && ./aws/install \
    && rm -rf awscliv2.zip aws

# Install CDK
RUN npm install -g aws-cdk@${CDK_VERSION} \
    && npm cache clean --force

# Install Inspector CLI https://docs.aws.amazon.com/inspector/latest/user/sbom-generator-versions.html
RUN curl "https://amazon-inspector-sbomgen.s3.amazonaws.com/$INSPECTOR_SBOMGEN/linux/amd64/inspector-sbomgen.zip" -o "/tmp/inspector-sbomgen.zip" \
    && unzip /tmp/inspector-sbomgen.zip -d /tmp/sbomgen_extracted \
    && mv /tmp/sbomgen_extracted/inspector-sbomgen-${INSPECTOR_SBOMGEN}/linux/amd64/inspector-sbomgen /usr/local/bin/inspector-sbomgen \
    && chmod +x /usr/local/bin/inspector-sbomgen \
    && rm -rf /tmp/inspector-sbomgen.zip /tmp/sbomgen_extracted

# Install Docker
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo \
    "deb [arch=\"$(dpkg --print-architecture)\" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
    bookworm stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
    docker-ce=${DOCKER_VERSION} \
    docker-ce-cli=${DOCKER_VERSION} \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Git
ADD https://github.com/git/git/archive/refs/tags/v${GIT_VERSION}.tar.gz /git/git-${GIT_VERSION}.tar.gz
RUN tar -C /git -xvf /git/git-${GIT_VERSION}.tar.gz \
    && make -C /git/git-${GIT_VERSION} prefix=/usr/local all \
    && make -C /git/git-${GIT_VERSION} prefix=/usr/local install \
    && rm -rf git

# Install JRE for SonarLint
RUN curl -OL ${JRE_URL} \
    && echo ${JRE_CKSUM} | sha256sum -c \
    && mkdir /usr/local/jre \
    && tar xzf ${JRE_TAR} -C /usr/local/jre --strip-components 1 \
    && rm ${JRE_TAR}

# Install Python dependencies
RUN python -m pip install --upgrade \
    invoke==${INVOKE_VERSION} \
    gnureadline==${GNUREADLINE_VERSION} \
    setuptools==${SETUPTOOLS_VERSION}  \
    pip==${PIP_VERSION} \
    pipenv==${PIPENV_VERSION} \
    uv==${UV_VERSION} \
    six wheel==${WHEEL_VERSION}

# === MCP server install (was data_reports_mcp_server/Dockerfile) =============

WORKDIR /app

COPY ./src/requirements.txt /app/requirements.txt

RUN uv pip install --no-cache --system --upgrade -r /app/requirements.txt

COPY ./src /app

ENV PORT=8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips '*'"]
