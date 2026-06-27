#include "mars_cam/webrtc_streamer.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstring>

namespace mars_cam {

namespace {
constexpr uint16_t kStunBindingRequest = 0x0001;
constexpr uint16_t kStunBindingSuccess = 0x0101;
constexpr uint16_t kStunAttrXorMappedAddress = 0x0020;
constexpr uint32_t kStunMagicCookie = 0x2112A442;
constexpr size_t kStunHeaderSize = 20;
constexpr size_t kStunTransactionIdOffset = 8;
constexpr size_t kStunTransactionIdSize = 12;

uint16_t read_be16(const uint8_t* p) {
    return static_cast<uint16_t>((static_cast<uint16_t>(p[0]) << 8) | p[1]);
}

uint32_t read_be32(const uint8_t* p) {
    return (static_cast<uint32_t>(p[0]) << 24) | (static_cast<uint32_t>(p[1]) << 16) |
           (static_cast<uint32_t>(p[2]) << 8) | static_cast<uint32_t>(p[3]);
}

void write_be16(uint8_t* p, uint16_t v) {
    p[0] = static_cast<uint8_t>((v >> 8) & 0xff);
    p[1] = static_cast<uint8_t>(v & 0xff);
}

void write_be32(uint8_t* p, uint32_t v) {
    p[0] = static_cast<uint8_t>((v >> 24) & 0xff);
    p[1] = static_cast<uint8_t>((v >> 16) & 0xff);
    p[2] = static_cast<uint8_t>((v >> 8) & 0xff);
    p[3] = static_cast<uint8_t>(v & 0xff);
}

bool is_stun_binding_request(const uint8_t* data, ssize_t len) {
    if (len < static_cast<ssize_t>(kStunHeaderSize)) {
        return false;
    }
    // STUN message type has the top two bits clear; this also filters RTP/DTLS-ish noise.
    if ((data[0] & 0xC0) != 0) {
        return false;
    }
    const uint16_t type = read_be16(data);
    const uint16_t body_len = read_be16(data + 2);
    const uint32_t cookie = read_be32(data + 4);
    if (type != kStunBindingRequest || cookie != kStunMagicCookie) {
        return false;
    }
    if ((body_len % 4) != 0) {
        return false;
    }
    return kStunHeaderSize + body_len <= static_cast<size_t>(len);
}

size_t build_ipv4_binding_response(
    const uint8_t* request, const sockaddr_in& from, std::array<uint8_t, 64>* out) {
    auto& response = *out;
    response.fill(0);

    write_be16(response.data(), kStunBindingSuccess);
    write_be16(response.data() + 2, 12);  // one XOR-MAPPED-ADDRESS attr: 4-byte header + 8-byte value
    write_be32(response.data() + 4, kStunMagicCookie);
    std::memcpy(response.data() + kStunTransactionIdOffset, request + kStunTransactionIdOffset,
                kStunTransactionIdSize);

    uint8_t* attr = response.data() + kStunHeaderSize;
    write_be16(attr, kStunAttrXorMappedAddress);
    write_be16(attr + 2, 8);
    attr[4] = 0;     // reserved
    attr[5] = 0x01;  // IPv4

    const uint16_t port = ntohs(from.sin_port);
    write_be16(attr + 6, static_cast<uint16_t>(port ^ (kStunMagicCookie >> 16)));

    const uint32_t addr = ntohl(from.sin_addr.s_addr);
    write_be32(attr + 8, addr ^ kStunMagicCookie);
    return kStunHeaderSize + 12;
}
}  // namespace

void WebRTCStreamer::start_local_stun_server() {
    if (!enable_local_stun_) {
        return;
    }
    if (local_stun_port_ <= 0 || local_stun_port_ > 65535) {
        RCLCPP_WARN(this->get_logger(), "Local STUN disabled: invalid port %d", local_stun_port_);
        return;
    }
    if (stun_running_.exchange(true)) {
        return;
    }
    stun_thread_ = std::thread(&WebRTCStreamer::stun_server_loop, this, static_cast<uint16_t>(local_stun_port_));
}

void WebRTCStreamer::stop_local_stun_server() {
    stun_running_.store(false);
    if (stun_fd_ >= 0) {
        shutdown(stun_fd_, SHUT_RDWR);
        close(stun_fd_);
        stun_fd_ = -1;
    }
    if (stun_thread_.joinable()) {
        stun_thread_.join();
    }
}

void WebRTCStreamer::stun_server_loop(uint16_t port) {
    const int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        RCLCPP_WARN(this->get_logger(), "Local STUN socket() failed: %s", std::strerror(errno));
        stun_running_.store(false);
        return;
    }

    int yes = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    sockaddr_in bind_addr{};
    bind_addr.sin_family = AF_INET;
    bind_addr.sin_addr.s_addr = htonl(INADDR_ANY);
    bind_addr.sin_port = htons(port);
    if (bind(fd, reinterpret_cast<sockaddr*>(&bind_addr), sizeof(bind_addr)) != 0) {
        RCLCPP_WARN(this->get_logger(), "Local STUN bind UDP :%u failed: %s", port, std::strerror(errno));
        close(fd);
        stun_running_.store(false);
        return;
    }

    stun_fd_ = fd;
    RCLCPP_INFO(this->get_logger(), "Local STUN Binding responder listening on UDP :%u", port);

    std::array<uint8_t, 1500> request{};
    std::array<uint8_t, 64> response{};
    while (stun_running_.load()) {
        pollfd pfd{fd, POLLIN, 0};
        const int ready = poll(&pfd, 1, 200);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        if (ready == 0 || !(pfd.revents & POLLIN)) {
            continue;
        }

        sockaddr_in from{};
        socklen_t from_len = sizeof(from);
        const ssize_t n = recvfrom(fd, request.data(), request.size(), 0, reinterpret_cast<sockaddr*>(&from),
                                   &from_len);
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN) {
                continue;
            }
            break;
        }
        if (from.sin_family != AF_INET || !is_stun_binding_request(request.data(), n)) {
            continue;
        }

        const size_t response_len = build_ipv4_binding_response(request.data(), from, &response);
        char ip[INET_ADDRSTRLEN] = {0};
        inet_ntop(AF_INET, &from.sin_addr, ip, sizeof(ip));
        RCLCPP_INFO(this->get_logger(), "Local STUN binding: %s:%u -> srflx %s:%u",
                    ip[0] ? ip : "?", ntohs(from.sin_port), ip[0] ? ip : "?", ntohs(from.sin_port));
        sendto(fd, response.data(), response_len, 0, reinterpret_cast<const sockaddr*>(&from), from_len);
    }

    if (stun_fd_ == fd) {
        stun_fd_ = -1;
        close(fd);
    }
    RCLCPP_INFO(this->get_logger(), "Local STUN Binding responder stopped");
}

}  // namespace mars_cam
