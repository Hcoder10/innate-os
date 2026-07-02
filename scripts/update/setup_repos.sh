#!/bin/bash
# Package repository setup for Innate-OS.
#
# Source this file to get setup_package_repos(), or execute it directly:
#     bash setup_repos.sh
#
# Configures the third-party apt repositories the install depends on:
#   - Innate packages   (ros-humble-innate-rws, etc.)
#
# Idempotent: the keyring + sources.list entries are only created when missing.
# Requires curl, gpg, and lsb_release on PATH.

# Log via the host script's log() *function* if defined (post_update.sh), else
# stdout. Uses `declare -F` so we don't accidentally match an external `log`
# binary (e.g. macOS /usr/bin/log).
_repos_log() {
    if declare -F log >/dev/null 2>&1; then
        log "$@"
    else
        echo "[setup_repos] $*"
    fi
}

setup_package_repos() {
    # Check if Innate packages repository is configured
    if [ ! -f /usr/share/keyrings/innate-archive-keyring.gpg ] || [ ! -f /etc/apt/sources.list.d/innate.list ]; then
        _repos_log "  Adding Innate packages repository..."

        # Add Innate GPG key
        if [ ! -f /usr/share/keyrings/innate-archive-keyring.gpg ]; then
            _repos_log "    Adding Innate GPG key..."
            curl -fsSL https://innate-inc.github.io/innate-packages/pubkey.gpg | \
                gpg --dearmor -o /usr/share/keyrings/innate-archive-keyring.gpg || {
                _repos_log "    ERROR: Failed to download Innate GPG key"
                exit 1
            }
        fi

        # Add Innate repository
        if [ ! -f /etc/apt/sources.list.d/innate.list ]; then
            _repos_log "    Adding Innate repository..."
            echo "deb [signed-by=/usr/share/keyrings/innate-archive-keyring.gpg] https://innate-inc.github.io/innate-packages/ $(lsb_release -cs) main" | \
                tee /etc/apt/sources.list.d/innate.list > /dev/null
        fi

        _repos_log "  Innate packages repository configured"
    else
        _repos_log "  Innate packages repository already configured"
    fi
}

# Execute when run directly (not when sourced).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    set -euo pipefail
    setup_package_repos
fi
