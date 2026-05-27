# syntax=docker/dockerfile:1.7
#
# Dockerfile for Maurice Robot (ROS 2 Humble) with zsh + oh-my-zsh.
#
# Keep this image focused on OS/runtime dependencies. The ROS workspace source
# is bind-mounted by sim/docker-compose.dev.yml and built into persistent named
# volumes at runtime, so source edits do not require rebuilding the image.

ARG ROS_BASE_IMAGE=ros:humble-ros-base
FROM ${ROS_BASE_IMAGE} AS os-deps

ENV DEBIAN_FRONTEND=noninteractive \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy \
    PIP_DISABLE_PIP_VERSION_CHECK=1

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# 1. Install base packages needed before apt-dependencies.txt.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        curl \
        iputils-ping \
        gnupg \
        lsb-release \
        ca-certificates \
        htop \
        btop

# 2. Add Innate packages repository (for ros-humble-innate-rws, etc.).
RUN curl -fsSL https://innate-inc.github.io/innate-packages/pubkey.gpg | \
        gpg --dearmor -o /usr/share/keyrings/innate-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/innate-archive-keyring.gpg] https://innate-inc.github.io/innate-packages/ $(lsb_release -cs) main" | \
        tee /etc/apt/sources.list.d/innate.list > /dev/null

# 3. Copy and install simulation apt dependencies.
COPY sim/apt-dependencies.txt /tmp/apt-dependencies.txt
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    grep -v '^#' /tmp/apt-dependencies.txt | grep -v '^$' | \
        xargs apt-get install -y --no-install-recommends && \
    rm /tmp/apt-dependencies.txt

# 4. Install uv for fast, cached Python package installation.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    ln -sf /root/.local/bin/uv /usr/local/bin/uv

# 5. Install Python runtime packages with uv. CPU PyTorch wheels avoid pulling
# multi-GB CUDA/NVIDIA packages into local Apple Silicon Docker builds. The
# trailing packaging/setuptools reinstall pins them after torch resolves its deps.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install --system \
        websockets \
        pydantic \
        'packaging>=24' \
        'setuptools==59.6.0' \
        'numpy<2' \
        h5py \
        cartesia \
        einops \
        python-dotenv \
        websocket-client && \
    uv pip install --system \
        --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple \
        --index-strategy unsafe-best-match \
        'torch==2.11.0+cpu' \
        'torchvision==0.26.0+cpu' && \
    uv pip install --system \
        'packaging>=24' \
        'setuptools==59.6.0'

# 6. Install oh-my-zsh (for root, since containers typically run as root unless changed).
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" \
    || true

# 7. Make zsh the default shell for root inside the container and use a compact theme.
RUN chsh -s /usr/bin/zsh root && \
    sed -i 's/ZSH_THEME="robbyrussell"/ZSH_THEME="agnoster"/' /root/.zshrc

# 8. Pre-approve GitHub SSH host key to avoid prompts.
RUN mkdir -p /root/.ssh && \
    ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null

FROM os-deps AS dev-runtime

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# sim/docker-compose.dev.yml bind-mounts the real source tree here. Creating the
# mount points keeps the image usable even before the bind mounts are attached.
RUN mkdir -p \
        /root/innate-os/ros2_ws \
        /root/innate-os/scripts \
        /root/innate-os/config \
        /root/innate-os/workspace

# Patch root .zshrc to source ROS, the workspace, and DDS only when present.
RUN cat >> /root/.zshrc <<'EOF'

# ----- Innate OS custom environment -----
[ -f /opt/ros/humble/setup.zsh ] && source /opt/ros/humble/setup.zsh
[ -f /root/innate-os/ros2_ws/install/setup.zsh ] && source /root/innate-os/ros2_ws/install/setup.zsh
[ -f /root/innate-os/config/dds/setup_dds.zsh ] && source /root/innate-os/config/dds/setup_dds.zsh
export INNATE_OS_ROOT=/root/innate-os
export CCACHE_DIR=/root/.cache/ccache
EOF

WORKDIR /root/innate-os
CMD ["zsh", "-l"]
