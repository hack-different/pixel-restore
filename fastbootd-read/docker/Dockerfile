# Linux build environment for a read-enabled fastbootd (komodo / Pixel 9 Pro XL).
#
# macOS cannot build AOSP or GrapheneOS directly: the platform build needs a
# case-sensitive filesystem and Linux-only host tools. Build inside this
# container instead. On Apple Silicon run it as linux/amd64 (see docker/build.sh
# and docs/build-and-boot.md) because the AOSP prebuilt host toolchain is
# x86_64; emulation is slower but works, and a native x86_64 Linux builder is
# faster still.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
# AOSP / GrapheneOS host build dependencies (see source.android.com and
# grapheneos.org/build). git-lfs, repo and a JDK are included.
RUN apt-get update && apt-get install -y --no-install-recommends \
      openjdk-21-jdk-headless \
      git git-lfs gnupg flex bison build-essential zip curl zlib1g-dev \
      libc6-dev-i386 libncurses5 lib32ncurses5-dev x11proto-core-dev libx11-dev \
      lib32z1-dev libgl1-mesa-dev libxml2-utils xsltproc unzip fontconfig \
      python3 python3-pip python-is-python3 ccache rsync kmod \
      libssl-dev bc cpio lz4 \
    && rm -rf /var/lib/apt/lists/*

# `repo` for syncing a manifest-based tree.
RUN curl -o /usr/local/bin/repo https://storage.googleapis.com/git-repo-downloads/repo \
    && chmod a+x /usr/local/bin/repo

# ccache speeds up repeat builds; point it at a mounted volume from build.sh.
ENV USE_CCACHE=1 CCACHE_EXEC=/usr/bin/ccache

WORKDIR /aosp
CMD ["/bin/bash"]
