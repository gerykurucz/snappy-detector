# SnappyAI report

Generated: 2026-11-07T23:33:25+00:00
Host: Windows 11 (build 26200)
CPU: Intel64 Family 6 Model 158 Stepping 10, GenuineIntel
GPU: NVIDIA GeForce GTX 1050
Primary NIC: Intel(R) Wireless-AC 9462 (Up)

## Latency budget

Four independent terms. A tool that reports only one of them misattributes problems.

| Component | What this run found |
|---|---|
| `local_queue` | not determined (2 check(s) returned UNKNOWN) |
| `network_path` | **net.wifi.prefer_wired; net.dns.fastest_resolver** |
| `host_processing` | **sched.background.lower_priority** |
| `render_input` | **gpu.vendor.low_latency_mode** |

## Changes recommended

### `net.wifi.prefer_wired` — Gaming over Wi-Fi instead of a cable

- component: `network_path` · confidence: **high** · risk: low · reboot: no
- finding: primary adapter Intel(R) Wireless-AC 9462 is wireless
- expected effect: Lower jitter and fewer latency spikes on the first hop.
- mechanism: A wireless medium is shared and half-duplex, so every frame contends for airtime, retries after interference, and waits behind neighbours traffic before the game packet even reaches the router queue.
- automated steps: 0
- verify with: `p95` over 200 samples; meaningful if >= 1.0 ms

### `net.dns.fastest_resolver` — Slow domain name resolver

- component: `network_path` · confidence: **medium** · risk: low · reboot: no
- finding: resolver ['1.1.1.1', '8.8.8.8', '1.1.1.1', '8.8.8.8'] took 82.2373000009975 ms
- expected effect: Faster connection setup for new sessions and services.
- mechanism: Every new game session, matchmaking call, and content fetch starts with a DNS lookup, and a slow or distant resolver adds its full round trip to connection setup before any game traffic flows.
- automated steps: 1
- verify with: `p95` over 200 samples; meaningful if >= 1.0 ms

### `gpu.vendor.low_latency_mode` — Graphics driver queues frames ahead

- component: `render_input` · confidence: **high** · risk: low · reboot: no
- finding: driver frame queueing on NVIDIA GeForce GTX 1050 was not confirmed optimal
- expected effect: Input appears on screen up to one frame sooner.
- mechanism: The driver holds one or more fully rendered frames in a flip queue to smooth over hitches, and each queued frame delays the visible reaction to an input by exactly one frame interval after the game already responded.
- automated steps: 0
- verify with: `p95` over 200 samples; meaningful if >= 1.0 ms

### `sched.background.lower_priority` — Background programs fight the game for CPU

- component: `host_processing` · confidence: **medium** · risk: low · reboot: no
- finding: background load competes with the game: python (15.0%)
- expected effect: Fewer preemptions while the heaviest competitor idles lower.
- mechanism: A background process burning whole cores forces the scheduler to time-slice the game thread, and each preemption evicts cache and delays interrupt handling by the length of the competing quantum.
- automated steps: 1
- verify with: `p95` over 200 samples; meaningful if >= 1.0 ms

## Checked and fine — no change needed

- `display.refresh_rate.maximum`: display runs at its maximum rate
- `capture.game_dvr.disable`: game recording is already off
- `input.usb.selective_suspend_off`: USB selective suspend is already disabled
- `timer.global_resolution_requests`: system tick is already the finest this platform allows (15.625 ms)
- `sched.mmcss.games_task`: multimedia scheduling profile present
- `cpu.cpu_sets.delegate`: single cache domain; roaming is cheap

## Could not be determined

Reported explicitly instead of substituted with a default.

- `nic.power_saving.disable`: adapter power management state could not be read
- `nic.interrupt_moderation.disable`: interrupt moderation state could not be read (the driver exposes no such property)
- `net.mtu.correctness`: path MTU was not measured; fragmentation cannot be judged yet
- `net.bufferbloat.upload_cap`: no loaded-latency measurement attached, so upload queueing is unjudged
- `net.bufferbloat.router_sqm`: no bufferbloat grade measured, so router queueing is unjudged
- `bus.pcie.aspm_off`: PCIe link power state cannot be read without elevated ACPI queries
- `cpu.interrupt_affinity.delegate`: interrupt-to-core routing cannot be read without elevated queries
- `os.game_mode.ab_test`: Game Mode effect cannot be read; only an A/B measurement decides

## Not applicable to this machine

- `gpu.hags.ab_test`: this GPU does not offer hardware-accelerated GPU scheduling (NVIDIA GeForce GTX 1050 predates it or the driver does not expose it) - there is nothing to toggle

## Refused by SnappyAI, and why

Widely recommended changes that SnappyAI will not generate.

### `kb.delay.speed` — Keyboard repeat delay and speed

Refused: keyboard repeat delay changes the Control Panel character repeat rate, not in-game input latency. No causal path to any budget term exists.

### `net.nagle.disable` — Disable Nagle's algorithm globally

Refused: per-connection TCP_NODELAY already covers games; a global override adds congestion risk with no measured benefit.

### `net.throttling.disable` — Disable network throttling index

Refused: the throttle protects multimedia traffic including games; disabling it helps bulk downloaders at the game's expense.

---

_SnappyAI measures, explains, and generates scripts for review. Negative results stay visible._