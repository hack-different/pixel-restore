# Linux build environment for a read-enabled fastbootd (komodo / Pixel 9 Pro XL).
#
# macOS cannot build AOSP or GrapheneOS directly: the platform build needs a
# case-sensitive filesystem and Linux-only host tools. Build inside this
# container instead. On Apple Silicon run it as linux/amd64 (see docker/build.sh
# and docs/build-and-boot.md) because the AOSP prebuilt host toolchain is
# x86_64; emulation is slower but works, and a native x86_64 Linux builder is
# faster still.
#
# Base: Debian bookworm (a GrapheneOS-supported build OS) with Node.js 24 LTS
# and yarn preinstalled, which adevtool needs to generate the Pixel vendor files.
FROM node:24-bookworm

ENV DEBIAN_FRONTEND=noninteractive
# AOSP / GrapheneOS host build dependencies (see source.android.com and
# grapheneos.org/build). The platform tree ships its own JDK and clang.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git git-lfs gnupg openssh-client flex bison build-essential zip unzip curl \
      zlib1g-dev libc6-dev-i386 libncurses5 lib32ncurses-dev x11proto-core-dev \
      libx11-dev lib32z1-dev libgl1-mesa-dev libxml2-utils xsltproc fontconfig \
      fonts-dejavu-core libfreetype6 python3 python-is-python3 ccache rsync kmod \
      libssl-dev bc cpio lz4 hostname procps \
    && rm -rf /var/lib/apt/lists/*

# `repo` for syncing the manifest-based tree.
RUN curl -fsSL -o /usr/local/bin/repo https://storage.googleapis.com/git-repo-downloads/repo \
    && chmod a+x /usr/local/bin/repo

# repo needs an identity, and the tree lives on a volume owned by another uid.
RUN git config --system user.name "fastbootd-read builder" \
    && git config --system user.email "builder@localhost" \
    && git config --system color.ui false \
    && git config --system --add safe.directory '*'

# ccache speeds up repeat builds; build.sh mounts a volume at /ccache.
ENV USE_CCACHE=1 CCACHE_EXEC=/usr/bin/ccache CCACHE_DIR=/ccache

WORKDIR /aosp
CMD ["/bin/bash"]
