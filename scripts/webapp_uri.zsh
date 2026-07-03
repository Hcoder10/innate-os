#!/bin/zsh
# Shared helper for printing the webapp URL on the robot's current Wi-Fi IP.

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
    local ip
    ip="$(innate_current_wifi_ip)"
    if [[ -z "$ip" ]]; then
        return 1
    fi
    print -r -- "https://$ip"
}

if [[ "${ZSH_EVAL_CONTEXT:-}" != *:file && "${1:-}" == "--print" ]]; then
    innate_webapp_uri
fi
