#!/bin/zsh
# Shared helper for printing the webapp URL as the robot's mDNS hostname
# (e.g. https://mars-the-27th.local), falling back to the Wi-Fi IP when no
# hostname is available. Wi-Fi connectivity is still required either way so
# the URL is only shown when the webapp is actually reachable.

innate_current_wifi_ip() {
    local iface ip

    for iface in $(nmcli -t -f DEVICE,TYPE,STATE device status 2>/dev/null | awk -F: '$2 == "wifi" && $3 == "connected" { print $1 }'); do
        ip="$(ip -o -4 addr show dev "$iface" scope global 2>/dev/null | awk '{ split($4, addr, "/"); print addr[1]; exit }')"
        if [[ -n "$ip" ]]; then
            print -r -- "$ip"
            return 0
        fi
    done

    for iface in $(iw dev 2>/dev/null | awk '$1 == "Interface" { print $2 }'); do
        ip="$(ip -o -4 addr show dev "$iface" scope global 2>/dev/null | awk '{ split($4, addr, "/"); print addr[1]; exit }')"
        if [[ -n "$ip" ]]; then
            print -r -- "$ip"
            return 0
        fi
    done
}

innate_webapp_uri() {
    local ip host
    ip="$(innate_current_wifi_ip)"
    if [[ -z "$ip" ]]; then
        return 1
    fi

    host="$(hostname -s 2>/dev/null)"
    if [[ -n "$host" ]]; then
        print -r -- "https://${host}.local"
    else
        print -r -- "https://$ip"
    fi
}

if [[ "${ZSH_EVAL_CONTEXT:-}" != *:file && "${1:-}" == "--print" ]]; then
    innate_webapp_uri
fi
