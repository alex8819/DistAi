package com.distai.app

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlin.math.pow

/**
 * Modello predittivo (speculative decoding) attivato solo quando conviene.
 * Una bozza piccola propone k token, il modello grande li verifica in un solo passaggio.
 * Guadagno = E / (k·t_bozza/t_1 + r(k)), con E = (1 − p^(k+1)) / (1 − p) token accettati per verifica,
 * t_1 il tempo di un token del modello grande e r(k) il costo di verificare k+1 token rispetto a uno.
 *
 * Sulle CPU r(k) cresce quasi come k (PC: r(4) ≈ 3,4) e la bozza peggiora (PC 7B: 4,5 → 3,0 tok/s).
 * Nel modello distribuito invece t_1 è dominato dalla rete, che si paga una volta sola per verifica:
 * lì r(k) resta basso e la bozza può rendere. Quindi si misura sul cluster vero:
 *  1. t_1 e r(k) dal coordinatore appena pronto (due richieste brevi);
 *  2. stima del guadagno con t_bozza dal benchmark del coordinatore e p dall'ultima misura (0,6 la prima volta);
 *  3. se la stima supera [MIN_GAIN] si riavvia con la bozza e si misura davvero; se non è più veloce si torna indietro.
 */
object Speculative {
    const val MIN_GAIN = 1.1
    private const val PROMPT = "Elenca in ordine i numeri da uno a cento scritti in lettere, separati da virgole:"

    data class Calib(val t1Ms: Double, val r: (Int) -> Double)
    data class Choice(val draft: File, val k: Int, val gain: Double)

    /** Bozza compatibile: stesso tokenizer, vocabolario quasi uguale (llama.cpp tollera 128 token), almeno 4 volte più piccola. */
    fun findDraft(models: List<File>, target: File, targetInfo: Gguf.Info): Pair<File, Gguf.Info>? =
        models.filter { it.path != target.path && it.length() * 4 <= target.length() }
            .mapNotNull { f -> Gguf.read(f)?.let { f to it } }
            .filter { (_, i) -> i.tokenizer.isNotEmpty() && i.tokenizer == targetInfo.tokenizer &&
                kotlin.math.abs(i.vocab - targetInfo.vocab) <= 128 }
            .minByOrNull { it.first.length() }

    /** Misura t_1 (generazione) e r(k) (un blocco di token verificato insieme) sull'API pronta in [api]. */
    fun calibrate(api: String): Calib? = runCatching {
        val gen = complete(api, PROMPT, 24)
        val t1 = gen.getJSONObject("timings").getDouble("predicted_per_token_ms")
        // blocco di m+1 token nuovi (senza cache): quanto costa rispetto a un token solo
        val batch = complete(api, "uno due tre quattro cinque sei sette otto", 1).getJSONObject("timings")
        val m = maxOf(1, batch.getInt("prompt_n") - 1)
        val rm = maxOf(1.0, batch.getDouble("prompt_ms") / t1)
        Calib(t1) { k -> 1 + (rm - 1) * k / m }
    }.onFailure { NodeState.log("bozza", "calibrazione non riuscita: ${it.message}") }.getOrNull()

    fun gain(k: Int, p: Double, tDraftMs: Double, c: Calib): Double {
        val e = (1 - p.pow(k + 1)) / (1 - p)
        return e / (k * tDraftMs / c.t1Ms + c.r(k))
    }

    /** Miglior k e guadagno stimato; [draftGbps] è la banda del nodo che esegue la bozza (il coordinatore). */
    fun choose(draft: File, draftGbps: Double, c: Calib, p: Double): Choice {
        val tDraft = draft.length() / 1e9 / maxOf(0.5, draftGbps) * 1000 + 1.0
        // con dotprod/i8mm i kernel ricompattati lavorano a blocchi di 4 token (Xiaomi Coder 3B: k=4 +13%, k=2 e k=6 peggio, k=3 si blocca):
        // il costo non è lineare in k, quindi la stima lineare di r(k) non vale e si prova solo k=4; la misura reale decide poi se tenerla
        val ks = if (Native.variant == "v8") (1..8).toList() else listOf(4)
        val k = ks.maxBy { gain(it, p, tDraft, c) }
        NodeState.log("bozza", "t_1 ${"%.0f".format(c.t1Ms)} ms · t_bozza ${"%.0f".format(tDraft)} ms · r($k) ${"%.2f".format(c.r(k))} · " +
            "p ${"%.2f".format(p)} → guadagno stimato ${"%.2f".format(gain(k, p, tDraft, c))} con k=$k")
        return Choice(draft, k, gain(k, p, tDraft, c))
    }

    /** Velocità reale con la bozza attiva e accettazione misurata. */
    fun measure(api: String): Pair<Double, Double?>? = runCatching {
        val t = complete(api, PROMPT, 48).getJSONObject("timings")
        val acc = if (t.optInt("draft_n") > 0) t.getDouble("draft_n_accepted") / t.getInt("draft_n") else null
        t.getDouble("predicted_per_second") to acc
    }.getOrNull()

    fun lastP(ctx: Context) = ctx.getSharedPreferences("distai", Context.MODE_PRIVATE).getFloat("spec_p", 0.6f).toDouble()
    fun saveP(ctx: Context, p: Double) =
        ctx.getSharedPreferences("distai", Context.MODE_PRIVATE).edit().putFloat("spec_p", p.toFloat().coerceIn(0.05f, 0.95f)).apply()

    fun complete(api: String, prompt: String, n: Int): JSONObject {
        val c = URL("$api/completion").openConnection() as HttpURLConnection
        c.requestMethod = "POST"; c.doOutput = true
        c.connectTimeout = 3000; c.readTimeout = 180_000
        c.setRequestProperty("Content-Type", "application/json")
        val body = JSONObject().put("prompt", prompt).put("n_predict", n).put("temperature", 0)
            .put("cache_prompt", false)
        c.outputStream.use { it.write(body.toString().toByteArray()) }
        return c.inputStream.bufferedReader().use { JSONObject(it.readText()) }
    }

    private fun healthy(api: String) = runCatching {
        val c = URL("$api/health").openConnection() as HttpURLConnection
        c.connectTimeout = 1000; c.readTimeout = 2000
        c.responseCode == 200
    }.getOrDefault(false)

    /** Aspetta che l'API risponda su /health (al massimo [timeoutMs]). */
    fun waitReady(api: String, timeoutMs: Long): Boolean {
        val end = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < end) {
            if (healthy(api)) return true
            Thread.sleep(1000)
        }
        return false
    }

    fun waitDown(api: String, timeoutMs: Long) {
        val end = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < end && healthy(api)) Thread.sleep(500)
    }
}
