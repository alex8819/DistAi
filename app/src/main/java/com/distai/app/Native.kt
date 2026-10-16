package com.distai.app

import android.app.ActivityManager
import android.content.Context
import android.os.Build
import java.io.File
import java.net.Inet4Address
import java.net.NetworkInterface

/** Scelta del binario llama.cpp ottimizzato per la CPU ARM del telefono e info di sistema. */
object Native {
    const val RPC_PORT = 50052
    const val API_PORT = 8080
    const val REPLICA_PORT = 8081
    const val STATUS_PORT = 50053
    const val KEEPALIVE_PORT = 50054

    /**
     * Variante della build: v86 (i8mm), v82 (dotprod+fp16) o v8 (baseline).
     * Feature del kernel: asimddp = dotprod, fphp = fp16, i8mm = int8 matmul.
     * Le feature sono richieste su tutti i core: /proc/cpuinfo le elenca per ogni core
     * e su big.LITTLE potrebbero differire, quindi controlliamo ogni riga "Features".
     */
    val variant: String by lazy {
        val perCore = runCatching {
            File("/proc/cpuinfo").readLines()
                .filter { it.startsWith("Features") }
                .map { it.substringAfter(':').trim().split(Regex("\\s+")).toSet() }
        }.getOrDefault(emptyList())
        fun all(vararg f: String) = perCore.isNotEmpty() && perCore.all { core -> f.all { it in core } }
        when {
            all("asimddp", "fphp", "i8mm") -> "v86"
            all("asimddp", "fphp") -> "v82"
            else -> "v8"
        }
    }

    /**
     * Flash attention solo dove la CPU ha fp16 (v82/v86): sullo Xiaomi (Dimensity 1080) +13% in generazione, sul Huawei (Kirin 970, v8)
     * -26% (misurato il 13/10/2026, vedi ROADMAP).
     */
    /**
     * Slot (richieste contemporanee) di una replica. Il calcolo a blocchi di 4 token è efficiente solo con dotprod/i8mm (kernel
     * ricompattati): Xiaomi Coder 3B Q4_0 8,0 tok/s con 1 richiesta e 22,8 con 4 (2,8 volte, ma 2 o 3 richieste non guadagnano nulla);
     * il Huawei (v8) resta a 4,2-4,6 con 1, 2 o 4 richieste (misurato il 13/10/2026). Quindi 4 slot oppure 1.
     */
    fun replicaSlots(): Int = if (variant == "v8") 1 else 4

    fun flashAttention(): List<String> = if (variant == "v8") emptyList() else listOf("-fa", "on")

    fun rpcServer(ctx: Context) = File(ctx.applicationInfo.nativeLibraryDir, "librpcserver_$variant.so")
    fun llamaServer(ctx: Context) = File(ctx.applicationInfo.nativeLibraryDir, "libllamaserver_$variant.so")

    fun cores(): Int = Runtime.getRuntime().availableProcessors()

    /** Thread consigliati: i core "big" (frequenza massima più alta), almeno 2. */
    fun bigCores(): Int {
        val freqs = (0 until cores()).mapNotNull { i ->
            runCatching {
                File("/sys/devices/system/cpu/cpu$i/cpufreq/cpuinfo_max_freq").readText().trim().toLong()
            }.getOrNull()
        }
        if (freqs.isEmpty()) return maxOf(2, cores() / 2)
        val min = freqs.min()
        return maxOf(2, freqs.count { it > min }.takeIf { it > 0 } ?: freqs.size)
    }

    /** Maschera esadecimale dei core big (es. "f0"), per `-C` e `GGML_RPC_CPUMASK`; null se non si legge cpufreq. */
    fun bigCoreMask(): String? {
        val freqs = (0 until cores()).map { i ->
            runCatching {
                File("/sys/devices/system/cpu/cpu$i/cpufreq/cpuinfo_max_freq").readText().trim().toLong()
            }.getOrNull() ?: return null
        }
        val min = freqs.min()
        val big = freqs.indices.filter { freqs[it] > min }.ifEmpty { freqs.indices.toList() }
        return big.fold(0L) { m, i -> m or (1L shl i) }.toString(16)
    }

    fun memInfo(ctx: Context): Pair<Long, Long> {
        val am = ctx.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val mi = ActivityManager.MemoryInfo().also { am.getMemoryInfo(it) }
        return (mi.totalMem / 1048576) to (mi.availMem / 1048576)
    }

    fun deviceName(): String = "${Build.MANUFACTURER} ${Build.MODEL}".trim()

    /** Primo IPv4 privato (Wi-Fi/Ethernet/hotspot) del dispositivo. */
    fun localIp(): String? = runCatching {
        // Wi-Fi ed Ethernet, poi la rete del cavo (tethering USB); mai i dati mobili: anche 10.x è "privato" ma è dell'operatore
        val mobile = listOf("rmnet", "ccmni", "pdp", "v4-rmnet")
        NetworkInterface.getNetworkInterfaces().toList()
            .filter { it.isUp && !it.isLoopback && mobile.none { m -> it.name.startsWith(m) } }
            .sortedBy {
                when {
                    it.name.startsWith("wlan") || it.name.startsWith("eth") -> 0
                    listOf("rndis", "ncm", "usb").any { p -> it.name.startsWith(p) } -> 1
                    else -> 2
                }
            }
            .flatMap { it.inetAddresses.toList() }
            .firstOrNull { it is Inet4Address && it.isSiteLocalAddress }
            ?.hostAddress
    }.getOrNull()

    /** IPv4 di questo telefono sulle reti su cavo: tethering USB (rndis/ncm/usb) o adattatore Ethernet USB-C (eth). */
    fun wiredIps(): List<String> = runCatching {
        NetworkInterface.getNetworkInterfaces().toList()
            .filter { i -> i.isUp && listOf("rndis", "ncm", "usb", "eth").any { i.name.startsWith(it) } }
            .flatMap { it.inetAddresses.toList() }.filterIsInstance<Inet4Address>().mapNotNull { it.hostAddress }
    }.getOrDefault(emptyList())

    fun wiredLink(): Boolean = wiredIps().isNotEmpty()

    fun summary(ctx: Context): String {
        val (tot, free) = memInfo(ctx)
        return "${deviceName()} · ${cores()} core ARM (${bigCores()} big) · build $variant\n" +
            "RAM ${tot} MB (libera ${free} MB) · IP ${localIp() ?: "nessuna rete"}"
    }
}
