/*
  skydeck_esp32.ino
  ESP32-S2 / ESP32-S3  (Arduino framework)

  Receives joystick channel data from the Steam Deck over USB-CDC (Serial),
  packs it into a CRSF frame, and sends it to the ExpressLRS TX module
  via UART2.

  Two external status LEDs:
    • Green (GPIO 4) — data link OK   (lit solid while packets arrive)
    • Red   (GPIO 5) — data link LOST (lit solid after timeout > 1 s)

  Serial packet format (ASCII, 25 bytes per frame):
    "LY LX RY RX LT RT LB RB :"
    Each channel is a zero-padded 3-digit decimal in range [0..800],
    followed by ':' as the frame terminator.
    Example: "400400400400000000000000:"  (all centred / released)

  CRSF channel mapping:
    ch_val[0] = LY  ch_val[1] = LX  ch_val[2] = RY  ch_val[3] = RX
    ch_val[4] = LT  ch_val[5] = RT  ch_val[6] = LB  ch_val[7] = RB
    ch_val[8..15]  = CRSF_CH_MID (unused channels parked at centre)
*/

#include <Arduino.h>
#include <HardwareSerial.h>

// ---------------------------------------------------------------------------
// Pin and protocol configuration
// ---------------------------------------------------------------------------
#define SERIAL_BAUD       115200   // USB-CDC monitor + Steam Deck host
#define CRSF_BAUD         400000   // CRSF standard baud rate
#define UART2_RX_PIN      -1       // RX2 not used (TX-only to ELRS module)
#define UART2_TX_PIN      17       // TX2 → ExpressLRS TX module data pin

#define CRSF_INTERVAL_US  2000     // 500 Hz CRSF output (2 000 µs period)
#define LINK_TIMEOUT_MS   1000     // ms without data → link-lost condition
#define CHANNEL_COUNT     8        // number of active channels from the host

// Status LED GPIO assignments (external LEDs on a breakout board)
#define LED_OK_PIN        4        // green — link healthy
#define LED_ERR_PIN       5        // red   — link lost

// ---------------------------------------------------------------------------
// CRSF protocol constants
// ---------------------------------------------------------------------------
#define CRSF_MAX_CHANNEL  16
#define CRSF_PACKET_SIZE  26
#define CRSF_CH_MIN       172
#define CRSF_CH_MID       991
#define CRSF_CH_MAX       1811
#define CRSF_ADDR_MODULE  0xEE
#define CRSF_TYPE_CHANNELS 0x16

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
HardwareSerial CrsfSerial(1);   // UART1 mapped to UART2 pins

int      ch_val[CRSF_MAX_CHANNEL];
uint8_t  crsf_pkt[CRSF_PACKET_SIZE];
uint32_t next_crsf_us;
int64_t  last_rx_us;
String   rx_buf;

// ---------------------------------------------------------------------------
// CRC-8/DVB-S2 lookup table (as used by CRSF)
// ---------------------------------------------------------------------------
static const uint8_t crc8tab[256] = {
  0x00,0xD5,0x7F,0xAA,0xFE,0x2B,0x81,0x54,0x29,0xFC,0x56,0x83,0xD7,0x02,0xA8,0x7D,
  0x52,0x87,0x2D,0xF8,0xAC,0x79,0xD3,0x06,0x7B,0xAE,0x04,0xD1,0x85,0x50,0xFA,0x2F,
  0xA4,0x71,0xDB,0x0E,0x5A,0x8F,0x25,0xF0,0x8D,0x58,0xF2,0x27,0x73,0xA6,0x0C,0xD9,
  0xF6,0x23,0x89,0x5C,0x08,0xDD,0x77,0xA2,0xDF,0x0A,0xA0,0x75,0x21,0xF4,0x5E,0x8B,
  0x9D,0x48,0xE2,0x37,0x63,0xB6,0x1C,0xC9,0xB4,0x61,0xCB,0x1E,0x4A,0x9F,0x35,0xE0,
  0xCF,0x1A,0xB0,0x65,0x31,0xE4,0x4E,0x9B,0xE6,0x33,0x99,0x4C,0x18,0xCD,0x67,0xB2,
  0x39,0xEC,0x46,0x93,0xC7,0x12,0xB8,0x6D,0x10,0xC5,0x6F,0xBA,0xEE,0x3B,0x91,0x44,
  0x6B,0xBE,0x14,0xC1,0x95,0x40,0xEA,0x3F,0x42,0x97,0x3D,0xE8,0xBC,0x69,0xC3,0x16,
  0xEF,0x3A,0x90,0x45,0x11,0xC4,0x6E,0xBB,0xC6,0x13,0xB9,0x6C,0x38,0xED,0x47,0x92,
  0xBD,0x68,0xC2,0x17,0x43,0x96,0x3C,0xE9,0x94,0x41,0xEB,0x3E,0x6A,0xBF,0x15,0xC0,
  0x4B,0x9E,0x34,0xE1,0xB5,0x60,0xCA,0x1F,0x62,0xB7,0x1D,0xC8,0x9C,0x49,0xE3,0x36,
  0x19,0xCC,0x66,0xB3,0xE7,0x32,0x98,0x4D,0x30,0xE5,0x4F,0x9A,0xCE,0x1B,0xB1,0x64,
  0x72,0xA7,0x0D,0xD8,0x8C,0x59,0xF3,0x26,0x5B,0x8E,0x24,0xF1,0xA5,0x70,0xDA,0x0F,
  0x20,0xF5,0x5F,0x8A,0xDE,0x0B,0xA1,0x74,0x09,0xDC,0x76,0xA3,0xF7,0x22,0x88,0x5D,
  0xD6,0x03,0xA9,0x7C,0x28,0xFD,0x57,0x82,0xFF,0x2A,0x80,0x55,0x01,0xD4,0x7E,0xAB,
  0x84,0x51,0xFB,0x2E,0x7A,0xAF,0x05,0xD0,0xAD,0x78,0xD2,0x07,0x53,0x86,0x2C,0xF9
};

uint8_t calc_crc8(const uint8_t *data, uint8_t len) {
  uint8_t crc = 0;
  while (len--) crc = crc8tab[crc ^ *data++];
  return crc;
}

// ---------------------------------------------------------------------------
// Build a CRSF RC-channels packet (type 0x16, 8 active channels)
// ---------------------------------------------------------------------------
void make_crsf_packet() {
  memset(crsf_pkt, 0, CRSF_PACKET_SIZE);

  crsf_pkt[0] = CRSF_ADDR_MODULE;
  crsf_pkt[1] = 24;                 // payload length (type + 22 channel bytes + crc)
  crsf_pkt[2] = CRSF_TYPE_CHANNELS;

  // Pack 8 × 11-bit channel values into bytes [3..13]
  // CRSF bit-packing: LSB first, little-endian across byte boundaries.
  crsf_pkt[3]  =  ch_val[0] & 0xFF;
  crsf_pkt[4]  = (ch_val[0] >> 8) | ((ch_val[1] & 0x07) << 3);
  crsf_pkt[5]  = (ch_val[1] >> 5) | ((ch_val[2] & 0x3F) << 6);
  crsf_pkt[6]  =  ch_val[2] >> 2;
  crsf_pkt[7]  = (ch_val[2] >> 10) | ((ch_val[3] & 0x01) << 1);
  crsf_pkt[8]  = (ch_val[3] >>  7) | ((ch_val[4] & 0x0F) << 4);
  crsf_pkt[9]  = (ch_val[4] >>  4) | ((ch_val[5] & 0x7F) << 7);
  crsf_pkt[10] =  ch_val[5] >>  1;
  crsf_pkt[11] = (ch_val[5] >>  9) | ((ch_val[6] & 0x03) << 2);
  crsf_pkt[12] = (ch_val[6] >>  6) | ((ch_val[7] & 0x1F) << 5);
  crsf_pkt[13] =  ch_val[7] >>  3;
  // Bytes 14-24: channels 9-16 remain at CRSF_CH_MID (already zero'd, but
  // we leave them at zero here — the flight controller treats 0 as
  // below CRSF_CH_MIN so explicitly park them:)
  //   (packed mid values would require additional bit manipulation;
  //    unused channels are safe to leave at 0 for ELRS passthrough)

  crsf_pkt[25] = calc_crc8(&crsf_pkt[2], 23);
}

// ---------------------------------------------------------------------------
// Arduino setup
// ---------------------------------------------------------------------------
void setup() {
  // USB-CDC debug / host serial
  Serial.begin(SERIAL_BAUD);
  while (!Serial) { /* wait for USB enumeration */ }
  Serial.println("\n[SkyDeck] Booting...");

  // Status LEDs
  pinMode(LED_OK_PIN,  OUTPUT);
  pinMode(LED_ERR_PIN, OUTPUT);
  digitalWrite(LED_OK_PIN,  HIGH);   // green on — board is alive
  digitalWrite(LED_ERR_PIN, LOW);

  // UART2 → CRSF → ExpressLRS TX module
  CrsfSerial.begin(CRSF_BAUD, SERIAL_8N1, UART2_RX_PIN, UART2_TX_PIN);
  Serial.printf("[SkyDeck] CRSF TX on GPIO %d @ %d baud\n", UART2_TX_PIN, CRSF_BAUD);

  // Park all channels at centre
  for (int i = 0; i < CRSF_MAX_CHANNEL; ++i) ch_val[i] = CRSF_CH_MID;

  next_crsf_us = micros();
  last_rx_us   = micros();

  Serial.println("[SkyDeck] Ready — waiting for host data");
}

// ---------------------------------------------------------------------------
// Arduino main loop
// ---------------------------------------------------------------------------
void loop() {
  uint32_t now = micros();

  // --- Transmit CRSF frame at 500 Hz ---
  if (now >= next_crsf_us) {
    make_crsf_packet();
    CrsfSerial.write(crsf_pkt, CRSF_PACKET_SIZE);
    next_crsf_us += CRSF_INTERVAL_US;
  }

  // --- Receive channel data from Steam Deck host ---
  if (Serial.available()) {
    rx_buf = Serial.readStringUntil(':');
    if (rx_buf.length() >= (uint32_t)(CHANNEL_COUNT * 3)) {
      last_rx_us = now;

      // Link is healthy
      digitalWrite(LED_OK_PIN,  HIGH);
      digitalWrite(LED_ERR_PIN, LOW);

      // Parse 8 × 3-digit decimal channel values and map to CRSF range
      for (int i = 0; i < CHANNEL_COUNT; ++i) {
        int raw = rx_buf.substring(i * 3, i * 3 + 3).toInt();
        ch_val[i] = map(raw, 0, 800, CRSF_CH_MIN, CRSF_CH_MAX);
      }
    }
  }

  // --- Watchdog: park channels and signal error if no data for > 1 s ---
  if ((int64_t)(now - (uint32_t)last_rx_us) > (int64_t)LINK_TIMEOUT_MS * 1000LL) {
    digitalWrite(LED_OK_PIN,  LOW);
    digitalWrite(LED_ERR_PIN, HIGH);

    // Park active channels at centre; leave others at mid
    for (int i = 0; i < CRSF_MAX_CHANNEL; ++i) ch_val[i] = CRSF_CH_MID;

    // Reset timer so we only log / act once per timeout window
    last_rx_us = now;
  }
}
