#!/bin/bash
# Package repository setup for Innate-OS.
#
# Source this file to get setup_package_repos(), or execute it directly:
#     bash setup_repos.sh
#
# Configures the third-party apt repositories the install depends on:
#   - Innate packages   (ros-humble-innate-rws, etc.)
#   - NodeSource 22.x    (nodejs, pinned to 22.* in apt-dependencies.common.txt;
#                         that pin only resolves with this repo present)
#
# Both steps are idempotent: the keyring + sources.list entries are only created
# when missing. Requires curl, gpg, and (for Innate) lsb_release on PATH.

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

    # Check if NodeSource repository is configured (Node.js 22.x for the webapp).
    # nodejs is pinned to 22.* in apt-dependencies.common.txt; that pin only
    # resolves with this repo present, so it must be configured before the install.
    if [ ! -f /usr/share/keyrings/nodesource.gpg ] || [ ! -f /etc/apt/sources.list.d/nodesource.list ]; then
        _repos_log "  Adding NodeSource repository (Node.js 22.x)..."

        if [ ! -f /usr/share/keyrings/nodesource.gpg ]; then
            _repos_log "    Adding NodeSource GPG key..."
            curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | \
                gpg --dearmor -o /usr/share/keyrings/nodesource.gpg || {
                _repos_log "    ERROR: Failed to download NodeSource GPG key"
                exit 1
            }
        fi

        if [ ! -f /etc/apt/sources.list.d/nodesource.list ]; then
            _repos_log "    Adding NodeSource repository..."
            echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" | \
                tee /etc/apt/sources.list.d/nodesource.list > /dev/null
        fi

        _repos_log "  NodeSource repository configured"
    else
        _repos_log "  NodeSource repository already configured"
    fi
}

# Execute when run directly (not when sourced).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    set -euo pipefail
    setup_package_repos
fi
