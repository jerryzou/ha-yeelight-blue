# Yeelight Blue II for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/jerryzou/ha-yeelight-blue/actions/workflows/validate.yml/badge.svg)](https://github.com/jerryzou/ha-yeelight-blue/actions/workflows/validate.yml)

A local Bluetooth integration for the **Yeelight Blue II**, the BLE-only RGB bulb Yeelight sold around 2015–2016 (E27, 6 W, 500 lm, FCC ID `2ABEUBUL27BL01`). It has no Wi-Fi, the official app and cloud are long gone, and neither the built-in Yeelight integration nor [hass-yeelightbt](https://github.com/hcoohb/hass-yeelightbt) (bedside lamp / Candela) supports it. These bulbs tend to outlive their software, so this gives them a second life.

- One `light` entity per bulb: on/off, brightness, RGB colour, and colour temperature from 1700 to 6500 K.
- Fully local, using Home Assistant's Bluetooth stack. It works through the host adapter or any [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html).
- Auto-discovered. There is no pairing and no button to press.
- Power-cut restore, quick offline detection and self-healing commands (details [below](#how-it-behaves)).

Not affiliated with Yeelight. Tested with three Blue II bulbs on Home Assistant 2026.10, through both the host's adapter and an ESP32 proxy.

Written by **Jerry Z & Claude** (Anthropic's AI, working in Claude Code): reverse-engineering notes, code and a day of power-cut tests on real bulbs.

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/jerryzou/ha-yeelight-blue`, category **Integration**.
2. Install **Yeelight Blue II**, then restart Home Assistant.

### Manual

Copy `custom_components/yeelight_blue` into your `config/custom_components/` folder, then restart Home Assistant.

## Setup

Power the bulb within range of a connectable Bluetooth adapter or proxy. It shows up under **Settings → Devices & services → Discovered** as *Yeelight Blue II XXXX* (the last four characters of its MAC address). Click **Add**. You can also add it manually: **Add integration → Yeelight Blue II** lists the bulbs in range.

Close any phone app that is connected to the bulb, because a bulb accepts only one connection at a time.

### Several bulbs in one fixture

Add each bulb, then create a **Group → Light group** helper over them. If the group takes over the entity ID your automations already use (for example, the smart plug the lamp hung off), nothing else needs to change.

## How it behaves

| | |
|---|---|
| **Connections** | Connects on demand and drops the link after 30 s idle, so it doesn't hold a proxy slot. |
| **Speed** | Commands are sent as write-without-response (≈15 ms on an open link; acknowledged writes take 0.1–2.7 s because the bulb answers after its fade). A status read 5 s later confirms the change landed. If an adapter refuses this, or the bulb ignores it, the integration falls back to acknowledged writes. |
| **Power cuts** | The bulb has no memory: it powers up at 4000 K, full brightness. The integration remembers the state last set from Home Assistant (across restarts) and re-applies it as soon as the bulb reappears, the same way a smart plug's "restore last state" works. So a lamp that was off stays off after a night-time power cut, apart from a few seconds of light. |
| **Offline detection** | Home Assistant's own Bluetooth tracker can take 5–15 min. Instead, if no advertisement or link activity is seen for 60 s, the integration probes the bulb with a status read, and only a failed probe marks it unavailable. On weak or noisy links, adverts can go missing for minutes, so silence alone isn't trusted. |
| **Weak links** | Each command is capped at 15 s and never raises, because an error would abort the calling automation and a light group waits for its slowest member. The intent is stored, and a failed command is re-sent by a status poll 60 s later and then every 5 min. |

A note on physical switches: Home Assistant's state wins. If HA has the lamp off and someone turns it on at the lamp's own switch, the bulb powers up at its default and the integration turns it back off. This design suits lamps that are controlled from Home Assistant.

## Protocol

The protocol is plain ASCII over GATT service `FFF0`, with no pairing or encryption. Every control write is padded with `,` to 18 bytes.

| Characteristic | Use | Payload |
|---|---|---|
| `FFF1` (write) | colour | `r,g,b,brightness` (brightness 0–100, 0 = off) |
| | brightness / power only | `,,,brightness` |
| | colour temperature | `CLTMP kelvin,brightness,` |
| `FFF5` (write) | status query | `S` |
| `FFF6` (notify) | status reply | `r,g,b,brightness,cf,dl,` or `CLTMP kelvin,brightness,` |

When the bulb is off in colour-temperature mode, its status reports a converted RGB value with brightness 0 (e.g. `71,61,11,0`). New connections are sometimes dropped by the bulb about 10 s in; writes are retried once over a fresh link.

Credit to [RootBlack04/Yeelight-Blue-II](https://github.com/RootBlack04/Yeelight-Blue-II), [sandeepmistry/node-yeelight-blue](https://github.com/sandeepmistry/node-yeelight-blue) and [peterlee0127/YeeLight-Blue-2](https://github.com/peterlee0127/YeeLight-Blue-2) for documenting it.

The first-generation Yeelight Blue (local name `Yeelight Blu`) appears to use the same protocol and is matched by discovery, but it is untested. Reports are welcome.

## Debugging

```yaml
logger:
  logs:
    custom_components.yeelight_blue: debug
```

At `info` level the log only records availability changes and power-cut restores. Debug adds every connect, write (with timing) and status reply.

## Authors

Jerry Z ([@jerryzou](https://github.com/jerryzou)) & Claude.

## License

MIT
