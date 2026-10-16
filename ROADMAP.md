# Roadmap

## Goal

Run an LLM on several ordinary phones, larger than any one of them can hold, and get **reliable tokens**: a steady, usable rate, not a lucky benchmark.
Models fit across phones today; the open problem is speed. The ideas below are the ones still worth testing, ordered by expected payoff.
Everything already measured, including what failed, is in [`NOTES.md`](NOTES.md).

## Why speed is the problem

Generating one token reads every active weight from RAM, so `tok/s ≈ memory bandwidth ÷ active bytes`. A phone reads 15-19 GB/s.
A pipeline split puts devices in series: the token time is the *sum* of the device times plus one network hop each, so it can never beat the fastest device that could hold the whole model.
Tensor parallelism would add the bandwidth of all phones, but it needs two synchronizations per layer (250-620 ms of network per token on Ethernet for a 27B model, against a budget of 20 ms).
Any new idea has to change one of three things: fewer bytes per token, more tokens per pass over the weights, or fewer network round trips per token.

## Ideas still to test

### 1. Diffusion LLMs (dLLM) on a cluster: most promising

A diffusion model produces many tokens per step. That moves the work from memory bandwidth to compute, and compute *adds up* across CPUs, which the autoregressive pipeline does not.

Done so far: a BD3LM block-diffusion engine on ggml (`native/patches-sperimentali/bd3-cli.patch`, Android build included) running a standard `qwen2` GGUF.
It reaches about 50 tok/s on the PC and 30 on phones. Splitting it by tensor over RPC works but is 5-7× slower because of latency; controlled 8B benchmarks did not reach the +20% target.

To test:
- **Fewer synchronizations per token.** Commit larger blocks or a lower confidence threshold, so one network round trip covers more tokens. Measure tokens per step against quality.
- **Bigger diffusion models** (8B and up). Larger matrices dilute fixed network costs; check when compute per step finally outweighs the round trip.
- **Wired link.** Repeat the split over USB tethering and USB-C Ethernet (0.3-1.5 ms) instead of Wi-Fi.
- **Pipeline instead of tensor split for diffusion.** Pass blocks through the stages and overlap them, since each step handles several tokens.
- **Reduced vocabulary head.** Already a gain for diffusion (-11% to -32% per step).
- **Other non-autoregressive approaches:** multi-token prediction heads, Medusa-style heads, and consistency or few-step distilled models.

Success criterion: at least +20% over the same model on the fastest single device, with the quality test (22 coding tasks) unchanged.

### 2. Distributed experts for large MoE models

Use phones as *memory for experts* of a mixture-of-experts model (Qwen3-30B-A3B, Gemma4-26B-A4B, Qwen3-235B-A22B), with a coordinator (a PC, ideally with a GPU) holding the dense part.
Per layer the coordinator sends 8-16 KB to the phones that hold the selected experts, in parallel, and sums the result. More phones means a bigger model at the same speed, not a slower one.
It needs a wired link: one round trip per layer is 240 ms for 48 layers on Wi-Fi, 25-50 ms on Ethernet.

| Setup | 30B-A3B | 235B-A22B (~40 phones) |
|---|---|---|
| Pipeline today | 5.3 tok/s (measured) | not tried |
| Distributed experts, Wi-Fi | ~3 | ~1.5 |
| Distributed experts, Ethernet, CPU coordinator | ~10-12 | ~3 |
| Distributed experts, Ethernet, GPU coordinator | ~15-25 | ~7 |

These are estimates, not measurements. Steps: **A** measure the network floor per token on 1 and 8 phones (Wi-Fi, adb, Ethernet), no code;
**B** prototype with the current llama.cpp using `-ot "exps=..."`; **C** real expert-parallel RPC in C++; **D** integrate into the app. Stop after A if the floor is too high.

### 3. Pipeline speculation (PipeInfer style)

In an N-stage pipeline with one conversation, each stage idles (N-1)/N of the time. Use that idle compute to run ahead on the guessed next tokens.
Expected speedup `N / (1 + (1-p)(N-1))` with acceptance rate p: 1.43× for 2 stages at p = 0.6, 2.2× for 10 stages. Unlike classic speculation it spends compute that is otherwise wasted, so it should work on compute-bound phones.
It needs changes to `ggml-rpc`, so it is a large project.

### 4. Recent phones

Everything was measured on old or mid-range devices. Phones with `i8mm`, LPDDR5X (50-70 GB/s) and Armv9 should change several verdicts:
speculative decoding and batching get cheaper with `i8mm`, KleidiAI may pay off (no gain on Exynos 9825), and bandwidth alone is 3-4× higher.
Re-run the key tests on a recent flagship and a Snapdragon with Adreno (OpenCL then becomes an option).

### 5. Smaller items

- [ ] Find the real memory limit per phone; the worker budget is 50% of `MemAvailable`.
- [ ] Distribute the 30B over several phones with tethering (only useful for quantizations that do not fit on the PC).
- [ ] Large batches (`-b/-ub 4096`) on the 30B with controlled free RAM.
- [ ] Several phones on cable at once; Note 10 with a full battery (Q4_0, `-fa on`, `--poll 100`, 4 slots, draft).
- [ ] Rebalance layers at runtime when a phone throttles.
- [ ] Lower the Vulkan requirement in ggml from 1.2 to 1.1 so that Mali GPUs (Vulkan 1.1) can be tried, or use ggml-webgpu.
- [ ] Compress weights (zstd) on first transfer.

## Not worth repeating

Adding pipeline nodes for speed, Skeleton-of-Thought, speculative decoding on phones without `i8mm`, n-gram drafts on code, rewriting the RPC protocol,
and promising dense 27B/70B at 50-100 tok/s on phones. Numbers are in [`NOTES.md`](NOTES.md).

## Related work

| Project | Idea worth borrowing |
|---|---|
| [Mesh-LLM](https://github.com/Mesh-LLM/mesh-llm) | split only if the model does not fit; RTT threshold to drop slow nodes; MoE expert sharding |
| prima.cpp (ICLR 2026) | device and layer selection (Halda); small models stay on the strongest device |
| [PhoneBorg](https://github.com/dzaczek/phoneborg) | replicas with session affinity; routing by temperature and battery |
| LinguaLinked (ACL 2024) | runtime layer rebalancing |
| [distributed-llama](https://github.com/b4rtaz/distributed-llama) | tensor parallelism with Q8 activations, wired networks only |
| [Cake](https://github.com/evilsocket/cake) | mDNS discovery, compressed weight transfer |
