# Notes: measurements, discarded ideas, device checklist

All figures were measured on the devices listed at the end of section 1, with temperature 0, one warm-up run and three timed runs, and the free RAM recorded.
Raw data: `tools/results-*.jsonl`. Quality is checked with `tools/codeeval.py` (22 coding tasks that are actually executed). Goals and open ideas: [`ROADMAP.md`](ROADMAP.md).

## 1. Results

### Rule of thumb
A token reads all active weights from RAM, so `tok/s ≈ bandwidth ÷ active bytes`. Cluster throughput with replicas is `η · Σ vᵢ / c(model)`, with η ≈ 0.85-0.95:
10 phones like the Huawei running a 0.5-1.5B model give 50-160 tok/s in total; the same cluster on a dense 7B gives about 11.

### Does distributing add speed?
For one conversation, no. The model is cut into slices and each token crosses them in turn.

| Model | Setup | tok/s |
|---|---|---|
| 0.5B | PC alone | **34** |
| 0.5B | PC + Samsung | 26 |
| 0.5B | 5 devices | 7.6 |
| 0.5B | Samsung alone / Huawei alone | 28 / 12-17 |
| 0.5B | Samsung + Huawei, Wi-Fi (12+12 layers) | 5 |
| 3B Q4_K_M | Huawei alone | 3.5 |
| 7B Q6_K (6.3 GB) | Samsung + Huawei | 0.8 (does not fit one phone) |
| 7B Q4_K_M | PC + Samsung, USB tethering, RPC patches | ~2 |
| 30B coder | PC alone / 12 layers on Xiaomi + Huawei | 7.0 / 3.9 |

Distribution is faster only when the model does not fit one device: a 7B on a PC without enough RAM rereads weights from disk and drops to fractions of a token per second, while the distributed run makes 2.63 tok/s.
Where tokens *are* gained is **more users**: in replica mode every device holds the full model and answers different users, so totals add up (79.6 tok/s with 11 users on PC, Samsung, Huawei and Pi).

### What did raise tokens

| Lever | Result |
|---|---|
| **MoE** (few active bytes) | LFM2-8B-A1B 22 tok/s on the PC vs 10 for the dense Coder 3B, similar quality (12/14 vs 13/14). Qwen3-Coder-30B-A3B at 2 bits (UD-IQ2_XXS, 10.3 GB) passes every task and runs at 7 tok/s on the PC, as long as free RAM exceeds the file (8.9 GB free: 5.3 tok/s; 10.7 GB: 7.0) |
| **Fewer active experts** | 8 → 6: +12-25% with no loss on 22 tasks; 5 loses 2 tasks; 4 collapses |
| **Repacked kernels** (Q4_0/Q4_K with dotprod) | prompt ×4.7; RPC workers lacked repacking, `rpc-repack.patch` brings prefill over RPC ×3-4 (Samsung 33 → 93-123 tok/s) with identical greedy output |
| **Q4_0 + flash attention + polling + pinned big cores** | Xiaomi +23% generation, +19% prompt (7.86 vs 6.39 tok/s) |
| **Batching** | Xiaomi Coder 3B: 8.0 tok/s with 1-3 requests, 22.8 with 4, 25.6 with 8. PC 9 → 18.8 with 4. Cluster with 9 users: **39.1 tok/s total** vs 10.2 for a single stream. MoE barely gains (21.7 → 26.5) |
| **Speculative decoding** | Xiaomi with a Coder 0.5B draft, k=4: +13% (8.94 vs 7.9). Needs the draft on the same threads and cores, polling off. Worse on the PC, Huawei and MoE |
| **Replicas + agent queue** | 22 coding tasks: 67.7 s on PC + Xiaomi + Huawei vs 203 s on the PC alone (3.0×) |
| **Weighted split** | planner (`SplitPlan.kt`, `tools/plan.py`) drops nodes that cost more than they give; Android free RAM is worth about half |
| **Output on the coordinator** | with the last layer on the phone each token sends ~600 KB of logits (3.5-5 tok/s). Phone first in the chain (`--rpc phone,pc`), output on the PC: 14-17 tok/s |

Prefill with a phone as a pipeline partner (Note 10 holds ~1/3 of the PC's prefill) is on par with the PC alone: the first +20% was noise. Always remeasure the reference.
Real case, a model that does not fit the PC (Qwen2.5 7B Q4_K_M, 14 layers PC + 14 Samsung over tethering): prefill of 2048 tokens 6.6 → 12.1 tok/s with the repack patch, and a full agent turn
(2048 tokens read, 300 written) 445 → 305 s (-31%). Startup 106 s, 65 s with weights cached on the phone.

### Core choice
- Generation depends on **RAM bandwidth**; the worker uses the 4 fastest cores and already reaches 18.7 GB/s, as much as all 8.
- Prompt reading is pure compute. The hypothesis "big cores only" was **disproved** end to end: all cores win (Xiaomi 82 vs 61 tok/s, Huawei 40 vs 27). Generation is the same with any mask.
- On the PC use one thread per physical core (4 cores: 208 GFLOPS; two cores with two threads each: 114).

| Note 10 (Exynos 9825) | GB/s | GFLOPS |
|---|---|---|
| 2 big (2.73 GHz) | 14.3 | 81 |
| 2 mid (2.40 GHz) | 6.3 | 32 |
| 4 little (1.95 GHz) | 2.4 | 24 |
| all 8 | 18.8 | 57 |

Huawei threads (0.5B, generation / prompt tok/s): `-t 2` 9.5/15, `-t 4` 16.3/30, `-t 6` 14.8/35, `-t 8` 16.5/38.

### Which nodes to exclude
Time per layer of a 7B Q4_K_M (134 MB), in ms: PC CPU 8.4 · Samsung 9.6 · Xiaomi 11.7 · PC integrated GPU 16.5 · Raspberry Pi 5 21.9.
- **Raspberry Pi 5:** exclude, unless the model does not fit without its RAM; then use Ethernet (over Wi-Fi it receives weights at 2 MB/s, 25 minutes for a 7B).
- **Integrated GPU:** exclude. It shares the CPU's RAM and a layer costs twice as much. A GPU makes sense only if dedicated, with its own memory.
- GPUs on phones: Mali-G76 exposes Vulkan 1.1 while ggml needs 1.2; ggml-opencl accepts only Adreno and Intel. Not usable so far.

### Links
| Link | Bandwidth | Latency |
|---|---|---|
| `adb forward` (current platform-tools) | 237 MB/s to the phone, 35 back | ~2 ms |
| `adb push` | 141 MB/s | |
| USB tethering (RNDIS) | 29.7 / 40.4 MB/s | 1.2-1.6 ms |
| Wi-Fi (5 GHz, strong signal) | 1.4-2 MB/s | 2-7 ms |
| `adb` 1.0.32 (old) | 3.9 MB/s | |

Use adb to load weights and tethering when the phone must be a network node. Wi-Fi limits *loading* the weights, not the token rate (one round trip and ~4 KB per token).

### Diffusion LLM (BD3LM on ggml)
About 50 tok/s on the PC and 30 on phones. Splitting by tensor over RPC works but is 5-7× slower because of latency; controlled 8B benchmarks missed the +20% target.
See `native/patches-sperimentali/README.md` and the diffusion item in [`ROADMAP.md`](ROADMAP.md).

### Physical limits
- A single stream cannot go faster than the best device that could hold the model.
- Dense 27B at 50-100 tok/s: bandwidth would need ~40 phones in tensor parallel, but then 124 synchronizations per token cost 250-620 ms on Ethernet and 4-7 s on Wi-Fi, against a 20 ms budget. Adding phones makes it worse.
  27B dense: 16.5 GB per token, 1.1 tok/s on one phone; 70B: 42.5 GB, 0.4 tok/s.
- Old phones are compute-bound (Kirin 970 tops out at 3.4 tok/s on a 3B); thermal throttling costs 20-30%; Android free RAM is worth half.

## 2. Tried and discarded

| Idea | Outcome |
|---|---|
| Distributing a model that fits one device | slower (30B: 3.9 vs 7.0 tok/s) |
| Skeleton-of-Thought (parallel branches) | 0.86-0.98×: the outline costs 10-16 s serially and each branch rereads the prompt |
| Draft model on PC, Huawei, MoE, or in the cluster | worse: checking k tokens costs about k times one token on weak CPUs |
| n-gram drafts on code | the first +29/+64% was a measurement artifact (same request repeated); nothing on new prompts |
| Reduced vocabulary head (autoregressive) | +1-7%: with 2-bit experts compute dominates, not bandwidth (helps diffusion, -11% to -32% per step) |
| Prompt on big cores only | disproved |
| KleidiAI on Note 10 | no gain (dotprod, no `i8mm`) |
| Flash attention on Kirin 970 | -26% (no fp16) |
| Prefill as a pipeline PC + phone | on par with the PC alone |
| Large batches (`-b/-ub 4096`) on the 30B | no clear gain (disturbed by free RAM) |
| Mobile GPUs (Mali with Vulkan 1.1; OpenCL only on Adreno/Intel; MediaTek has no Adreno) | not usable |

**Method:** never benchmark a technique by repeating the same prompt (cache and n-grams); record the number of requests (1, 2, 3, 4, 8) and the free RAM before blaming the code; always pair speed with a quality test that really runs.

## 3. Device checklist

Each vendor (Samsung One UI, Huawei EMUI, Xiaomi MIUI/HyperOS, Pixel, ColorOS) and Android version differs in power saving, networking and memory. Verify on every phone.
Legend: ✅ verified · ⚠️ works with workarounds · ❌ broken · – untested.

### Wi-Fi power save (huge impact on tok/s)
An idle phone puts the Wi-Fi radio to sleep and the router delivers incoming packets only at the next beacon. Measured on the Mate 10 Pro (EMUI, Android 10):

| Situation | Mean latency | Max |
|---|---|---|
| Idle phone, 1 ping/s from the PC | 205 ms | 539 ms |
| App UDP heartbeat, outbound only | 14.6 ms | 146 ms |
| Bidirectional heartbeat (as in the cluster) | **5.6 ms** | **7 ms** |

`Keepalive.kt` sends a 1-byte UDP packet every 100 ms on port 50054 between coordinator and worker. To check a phone:
```bash
ping -c 15 -i 1 PHONE_IP                                                     # without the app: any spikes?
python3 tools/udpkeep.py PHONE_IP 0.1 25 & ping -c 20 -i 1 PHONE_IP          # with the heartbeat
```

### Standby, Doze, background kills
- Mate 10 Pro enters standby after about 10 minutes. Fixes: "Keep screen on" in the app (on by default), or `adb shell svc power stayon usb` while charging (undo with `svc power stayon false`).
- Check on each phone: the worker survives 30 minutes with the screen off; the battery-optimization exemption is respected; vendor killers (Huawei "App launch", Xiaomi "Autostart", Samsung "Sleeping apps");
  Android 12+ **phantom process killer** (`logcat | grep -i phantom`; fix `adb shell device_config put activity_manager max_phantom_processes 2147483647`). See https://dontkillmyapp.com.

### Memory
- Check total RAM and real `MemAvailable` (app status on port 50053). The app refuses oversized models with a 400 MB reserve plus 200 MB for compute (tuned on the Mate 10 Pro: a 3B Q4_K_M, 2.1 GB, runs with 2.85 GB available).
- **Never load a model larger than usable RAM**: the Note 10 shut down loading 6.3 GB alone.
- Worker weights live in anonymous RAM and end up in swap/zram. On coordinators prefer internal storage to `/sdcard` (FUSE is slow with mmap before Android 12).

### CPU
- Verify the automatic variant (`v8`/`v82`/`v86`) against `/proc/cpuinfo` features (`asimddp`, `fphp`, `i8mm`).
- Tune `-t` (generation) and `-tb` (prompt); Mate 10 Pro: `-t 4 ≈ -t 8`, `-tb 8` gives +25% on the prompt. Check throttling after 10 minutes under load.
- Check thread placement of the worker launched by the app (`adb shell ps -T -o TID,PSR,%CPU -p PID`). On the Note 10 four threads landed on two cores (worker 4× slower than from adb);
  the app now pins threads to the big cores (`GGML_RPC_CPUMASK`) and the coordinator uses `--poll 100`.

### Binaries and discovery
- `nativeLibraryDir` must be executable (W^X); tested on Android 10 and 12.
- mDNS/NSD: some routers block multicast or enable client isolation. mDNS does not cross networks, so phones on different Wi-Fi networks cannot see each other.

### Results per phone

| Check | Note 10 (One UI, A12) | Mate 10 Pro (EMUI, A10) |
|---|---|---|
| CPU variant | v82 ✅ | v8 ✅ |
| Executable binaries | ✅ | ✅ |
| mDNS announce/discover | ✅ | ✅ |
| Wi-Fi power save + heartbeat | ✅ ping 95/508 → ~15 ms | ✅ 205 → 5.6 ms |
| Standby / screen on | – | ⚠️ standby every 10 min → `stayon` |
| Survives screen off | – | – |
| Phantom process killer | – | n/a (A10) |
| RAM check | ❌ shut down without it (before the fix) | ✅ tuned |
| Best threads (`-t`/`-tb`) | 4 / ? | 4 / 8 |
| Thermal throttling | – | – |
| Worker threads from the app | ❌ 4 threads on 2 cores | – |

### Test devices
Samsung Galaxy Note 10 (Exynos 9825, 7.4 GB, v82, Android 12) · Huawei Mate 10 Pro (Kirin 970, 5.8 GB, v8, Android 10) · Xiaomi Redmi Note 12 Pro (MediaTek, v82) ·
Raspberry Pi 4 (Cortex-A72, 3.8 GB) and Pi 5 · Huawei P40 Lite E (Kirin 710, 4 GB, no dotprod) · PC i7-7700 with HD 630 (11 GB free RAM).
