package com.distai.app

import android.content.Context
import android.net.wifi.WifiManager
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import kotlin.concurrent.thread

/**
 * Bench di questo dispositivo all'avvio, pubblicato in /status ("bench") così ogni nodo conosce gli altri senza misurarli:
 * - il worker così com'è configurato (banda per la generazione, GFLOPS per il prompt);
 * - ogni gruppo di core (big / medi / little / tutti) con un worker di prova sulla porta [TEST_PORT];
 * - il collegamento Wi-Fi (banda, velocità, segnale).
 * Misurato il 12/10/2026 (tools/devbench.py): sul Note 10 i 2 big danno 14,3 GB/s e 81 GFLOPS, tutti gli 8 core 18,8 GB/s
 * ma 57 GFLOPS: la generazione vuole più core, il prompt solo i big.
 */
object SelfBench {
    private const val TEST_PORT = 50059

    /** ultimo risultato (anche quello salvato dall'avvio precedente, finché il nuovo non è pronto) */
    @Volatile var result: JSONObject? = null
        private set
    @Volatile var running = false
        private set

    fun load(ctx: Context) {
        if (result == null) prefs(ctx).getString("bench", null)?.let { result = runCatching { JSONObject(it) }.getOrNull() }
    }

    /** Avvia il bench in background se il worker è libero (nessun coordinatore collegato). */
    fun start(ctx: Context) {
        if (running) return
        running = true
        thread(name = "selfbench") {
            try {
                Thread.sleep(3000) // il worker appena avviato deve aprire la porta
                if (WorkerStats.clients > 0 || NodeState.coordinatorRunning) {
                    NodeState.log("bench", "worker occupato: bench rimandato"); return@thread
                }
                NodeState.log("bench", "misuro questo dispositivo (circa 20 s)…")
                val j = JSONObject().put("at", System.currentTimeMillis())
                RpcBench.run("127.0.0.1", Native.RPC_PORT)?.let { j.put("gbps", it.gbps) }
                RpcBench.compute("127.0.0.1", Native.RPC_PORT)?.let { j.put("gflops", it) }
                j.put("groups", JSONArray(groups(ctx)))
                wifi(ctx)?.let { j.put("wifi", it) }
                result = j
                prefs(ctx).edit().putString("bench", j.toString()).apply()
                NodeState.log("bench", describe(j))
            } catch (e: Exception) {
                NodeState.log("bench", "errore: ${e.message}")
            } finally {
                running = false
                NodeState.notifyChanged()
            }
        }
    }

    /** Gruppi di core per frequenza massima (dal più veloce) più "tutti": banda e GFLOPS con un worker di prova. */
    private fun groups(ctx: Context): List<JSONObject> {
        val byFreq = (0 until Native.cores()).groupBy { i ->
            runCatching { File("/sys/devices/system/cpu/cpu$i/cpufreq/cpuinfo_max_freq").readText().trim().toLong() }.getOrDefault(0L)
        }.toSortedMap(compareByDescending { it })
        val names = listOf("big", "medi", "little", "altri")
        val list = byFreq.entries.mapIndexed { i, (freq, cpus) ->
            Triple("${names[minOf(i, 3)]} ${cpus.size}×${"%.2f".format(freq / 1e6)} GHz", cpus, cpus.size)
        }.toMutableList()
        if (list.size > 1) list += Triple("tutti ${Native.cores()}", (0 until Native.cores()).toList(), Native.cores())
        return list.map { (name, cpus, n) ->
            val mask = cpus.sumOf { 1L shl it }.toString(16)
            val j = JSONObject().put("name", name).put("mask", mask).put("threads", n)
            val p = runCatching {
                ProcessBuilder(Native.rpcServer(ctx).path, "-H", "127.0.0.1", "-p", TEST_PORT.toString(), "-t", n.toString())
                    .redirectErrorStream(true).apply { environment()["GGML_RPC_CPUMASK"] = mask }.start()
            }.getOrNull() ?: return@map j
            try {
                Thread.sleep(1500)
                RpcBench.run("127.0.0.1", TEST_PORT)?.let { j.put("gbps", it.gbps) }
                RpcBench.compute("127.0.0.1", TEST_PORT)?.let { j.put("gflops", it) }
            } finally {
                p.destroy()
            }
            j
        }
    }

    @Suppress("DEPRECATION")
    private fun wifi(ctx: Context): JSONObject? = runCatching {
        val i = (ctx.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager).connectionInfo
        // networkId è -1 senza il permesso di posizione (Android 10+): la frequenza invece c'è sempre se collegato
        if (i.frequency <= 0) null else JSONObject()
            .put("ghz", when { i.frequency > 5900 -> 6; i.frequency > 4900 -> 5; else -> 2.4 })
            .put("mbps", i.linkSpeed).put("rssi", i.rssi)
    }.getOrNull()

    /** riga leggibile, per il log e la scheda del dispositivo */
    fun describe(j: JSONObject?): String {
        if (j == null) return ""
        val f = { v: Double -> "%.1f".format(v) }
        val g = j.optJSONArray("groups")?.let { a ->
            (0 until a.length()).map { a.getJSONObject(it) }.filter { it.has("gbps") }
                .joinToString(" · ") { "${it.optString("name")}: ${f(it.optDouble("gbps"))} GB/s ${"%.0f".format(it.optDouble("gflops"))} GFLOPS" }
        }.orEmpty()
        val w = j.optJSONObject("wifi")?.let { " · Wi-Fi ${it.opt("ghz")} GHz ${it.optInt("mbps")} Mbps ${it.optInt("rssi")} dBm" }.orEmpty()
        return "Bench: worker ${f(j.optDouble("gbps", 0.0))} GB/s · ${"%.0f".format(j.optDouble("gflops", 0.0))} GFLOPS$w" +
            if (g.isNotEmpty()) "\nCore: $g" else ""
    }

    private fun prefs(ctx: Context) = ctx.getSharedPreferences("distai", Context.MODE_PRIVATE)
}
