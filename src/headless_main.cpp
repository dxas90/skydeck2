/*
 * headless_main.cpp — SkyDeck headless WFB-NG receiver
 *
 * Pipeline (mirrors Aviateur wfbng_link.cpp, no GUI):
 *   RTL8812AU (libusb/devourer) → RxFrame filter → AggregatorUDPv4 → UDP port
 *
 * Key fixes vs previous version:
 *   - Use RxFrame::IsValidWfbFrame() + MatchesChannelID() to filter frames
 *   - Strip ieee80211_header (24 bytes) + FCS (4 bytes) before process_packet
 *   - Use AggregatorUDPv4 directly (handles UDP output internally)
 *   - Use devourer::find_wifi_interface + claim_interface_then_reset for USB
 *
 * Env vars:
 *   SKYDECK_CHANNEL   WiFi channel (default 161)
 *   SKYDECK_BW        0=20MHz 1=40MHz (default 0)
 *   SKYDECK_PORT      UDP output port (default 5600)
 *   SKYDECK_LINK_ID   WFB-NG link_id (default 7669206)
 *   SKYDECK_KEY       gs.key path (default ~/.aviateur/gs.key)
 */
#include <arpa/inet.h>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>

#include <libusb-1.0/libusb.h>
#include "WiFiDriver.h"
#include "RxPacket.h"
#include "SelectedChannel.h"
#include "UsbOpen.h"
#include "logger.h"
#include "rx_frame.h"
#include "wfb-ng/rx.hpp"
#include "wfb-ng/protocol/wifibroadcast.hpp"

static std::unique_ptr<IRtlDevice> g_device;

static void on_signal(int) {
    if (g_device) g_device->StopRxLoop();
}

static int         ei(const char *n, int d)         { auto v=std::getenv(n); return v?std::atoi(v):d; }
static std::string es(const char *n, std::string d) { auto v=std::getenv(n); return v?v:d; }
static uint32_t    eu(const char *n, uint32_t d)    { auto v=std::getenv(n); return v?(uint32_t)std::stoul(v):d; }

int main() {
    std::signal(SIGINT,  on_signal);
    std::signal(SIGTERM, on_signal);

    const int      ch_num  = ei("SKYDECK_CHANNEL", 161);
    const int      bw_idx  = ei("SKYDECK_BW",        0);
    const int      port    = ei("SKYDECK_PORT",    5600);
    const uint32_t link_id = eu("SKYDECK_LINK_ID", 7669206);
    std::string    key     = es("SKYDECK_KEY",
        std::string(std::getenv("HOME") ? std::getenv("HOME") : "") + "/.aviateur/gs.key");

    fprintf(stderr, "[skydeck_rx] ch=%d bw=%s key=%s port=%d link_id=%u\n",
            ch_num, bw_idx ? "40MHz" : "20MHz", key.c_str(), port, link_id);

    // channel_id = (link_id << 8) + video_radio_port (0)
    // stored big-endian for MatchesChannelID()
    const uint32_t video_channel_id_f  = (link_id << 8) + 0;
    uint32_t       video_channel_id_be = htobe32(video_channel_id_f);
    const uint8_t *ch_id_be8 = reinterpret_cast<const uint8_t *>(&video_channel_id_be);

    // AggregatorUDPv4: handles decrypt+FEC and sends decoded RTP to UDP
    auto agg = std::make_unique<AggregatorUDPv4>(
        "127.0.0.1", port, key, /*epoch=*/0, video_channel_id_f, /*snd_buf=*/0);

    std::mutex agg_mutex;

    // USB open — use devourer's proper open sequence
    libusb_context *ctx = nullptr;
    if (libusb_init(&ctx) != 0) {
        fputs("[skydeck_rx] libusb_init failed\n", stderr); return 1;
    }

    auto *handle = libusb_open_device_with_vid_pid(ctx, 0x0bda, 0x8812);
    if (!handle) {
        fputs("[skydeck_rx] RTL8812AU (0bda:8812) not found\n", stderr);
        libusb_exit(ctx); return 1;
    }

    auto logger = std::make_shared<Logger>();
    int iface = devourer::find_wifi_interface(handle);

    std::shared_ptr<devourer::UsbDeviceLock> usb_lock;
    int rc = devourer::claim_interface_then_reset(handle, iface, logger, /*do_reset=*/false, usb_lock);
    if (rc < 0) {
        fprintf(stderr, "[skydeck_rx] claim_interface failed: %d\n", rc);
        libusb_close(handle); libusb_exit(ctx); return 1;
    }

    WiFiDriver drv{logger};
    g_device = drv.CreateRtlDevice(handle, ctx, usb_lock);
    if (!g_device) {
        fputs("[skydeck_rx] CreateRtlDevice failed\n", stderr);
        libusb_close(handle); libusb_exit(ctx); return 1;
    }

    SelectedChannel selected_ch{
        (uint8_t)ch_num,
        (uint8_t)0,
        bw_idx ? CHANNEL_WIDTH_40 : CHANNEL_WIDTH_20
    };

    fprintf(stderr, "[skydeck_rx] Listening -> udp://127.0.0.1:%d\n", port);

    static int8_t  rssi[2]    = {1, 1};
    static int8_t  noise[4]   = {1, 1, 1, 1};
    static uint8_t antenna[4] = {1, 1, 1, 1};

    g_device->Init(
        [&](const Packet &pkt) {
            // pkt.Data is the raw 802.11 frame (no radiotap — devourer strips it)
            const RxFrame frame(pkt.Data);

            // Filter: must be a valid WFB-NG data frame for our link_id
            if (!frame.IsValidWfbFrame()) return;
            if (!frame.MatchesChannelID(ch_id_be8)) return;

            // Strip 802.11 header (24 bytes) and FCS (4 bytes) — mirrors wfbng_link.cpp
            const size_t hdr = sizeof(ieee80211_header);
            const size_t fcs = 4;
            if (pkt.Data.size() <= hdr + fcs) return;

            std::lock_guard<std::mutex> lock(agg_mutex);
            agg->process_packet(
                pkt.Data.data() + hdr,
                pkt.Data.size() - hdr - fcs,
                0, antenna, rssi, noise,
                0, 0, 0, nullptr);
        },
        selected_ch);

    // Clean shutdown
    if (g_device) { g_device->Stop(); g_device.reset(); }
    libusb_release_interface(handle, iface);
    libusb_close(handle);
    libusb_exit(ctx);
    return 0;
}
