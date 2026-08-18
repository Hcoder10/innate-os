#!/bin/bash
# Build the innate-gstreamer-opt Debian package: a self-contained GStreamer at
# /opt/gst that unlocks webrtcbin's ULPFEC (broken on the system 1.20 — see
# docs/WEBRTC_FEC_GST_UPGRADE.md). Run natively on a Jetson (arm64); the deb
# then goes to the innate-packages repo root, whose publish.sh signs and
# indexes every *.deb it finds. Robots pick it up through the normal update
# flow via apt-dependencies.hardware.txt, and camera_composable.launch.py
# activates /opt/gst automatically when present.
#
#   ./scripts/update/build_gst_opt.sh            # -> innate-gstreamer-opt_<v>-1_arm64.deb
#   GST_VERSION=1.24.13 ./scripts/update/build_gst_opt.sh
set -euo pipefail

V="${GST_VERSION:-1.24.12}"
WORK="${WORK_DIR:-$HOME/gst-src}"
PREFIX=/opt/gst

sudo apt-get install -y ninja-build flex bison libssl-dev libsrtp2-dev \
  libvpx-dev liborc-dev libjpeg-dev python3-pip pkg-config cmake curl
pip3 install --user "meson>=1.1"
export PATH="$HOME/.local/bin:$PATH"

mkdir -p "$WORK" && cd "$WORK"
if [ ! -d "gstreamer-$V" ]; then
  curl -fsSL -o gst.tar.gz \
    "https://gitlab.freedesktop.org/gstreamer/gstreamer/-/archive/$V/gstreamer-$V.tar.gz"
  tar xf gst.tar.gz
fi
cd "gstreamer-$V"

meson setup build --prefix="$PREFIX" -Dbuildtype=release -Dgpl=disabled \
  -Dugly=disabled -Dlibav=disabled -Ddevtools=disabled -Dges=disabled \
  -Drtsp_server=disabled -Ddoc=disabled -Dexamples=disabled -Dtests=disabled \
  -Dintrospection=disabled -Dpython=disabled -Dlibnice=enabled \
  -Dgst-plugins-bad:webrtc=enabled -Dgst-plugins-bad:dtls=enabled \
  -Dgst-plugins-bad:srtp=enabled -Dgst-plugins-bad:sctp=enabled \
  -Dgst-plugins-good:vpx=enabled -Dgst-plugins-good:rtpmanager=enabled
ninja -C build

ROOT="$WORK/debroot"
rm -rf "$ROOT" && mkdir -p "$ROOT$PREFIX" "$ROOT/DEBIAN"
DESTDIR="$ROOT" meson install -C build --no-rebuild

# The gate that matters: every WebRTC element present, and NVIDIA's hardware
# plugins (built against the system gst) loading under this core.
export LD_LIBRARY_PATH="$ROOT$PREFIX/lib/aarch64-linux-gnu"
export GST_PLUGIN_PATH="$ROOT$PREFIX/lib/aarch64-linux-gnu/gstreamer-1.0:/usr/lib/aarch64-linux-gnu/gstreamer-1.0"
export GST_REGISTRY="$WORK/verify.registry"
B="$ROOT$PREFIX/bin/gst-inspect-1.0"
for el in webrtcbin vp8enc rtpvp8pay srtpenc dtlssrtpenc rtpulpfecenc rtpredenc \
          nicesink nvv4l2decoder nvvidconv nvjpegenc; do
  "$B" "$el" > /dev/null || { echo "VERIFY FAILED: $el"; exit 1; }
done

cat > "$ROOT/DEBIAN/control" << EOF
Package: innate-gstreamer-opt
Version: $V-1
Section: libs
Priority: optional
Architecture: arm64
Depends: libc6, libglib2.0-0, libsrtp2-1, libvpx7, liborc-0.4-0, libjpeg-turbo8, libssl3, zlib1g
Maintainer: Innate Inc <ops@innate.bot>
Description: Parallel GStreamer $V runtime at /opt/gst for WebRTC FEC
 Self-contained GStreamer (core/base/good/bad + libnice) under /opt/gst.
 The camera stack opts in via camera_composable.launch.py when this
 directory exists; without it robots run the system GStreamer unchanged.
 Ships working webrtcbin ULPFEC (broken on system 1.20).
EOF
dpkg-deb --build --root-owner-group "$ROOT" \
  "$WORK/innate-gstreamer-opt_${V}-1_arm64.deb"
echo "OK: $WORK/innate-gstreamer-opt_${V}-1_arm64.deb"
