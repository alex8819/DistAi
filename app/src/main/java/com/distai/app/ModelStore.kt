package com.distai.app

import android.content.Context
import java.io.File
import java.io.RandomAccessFile
import java.net.HttpURLConnection
import java.net.URL

/** Modelli GGUF: scaricati da URL o copiati via USB in Android/data/com.distai.app/files/models. */
object ModelStore {
    data class Preset(val label: String, val url: String)

    val presets = listOf(
        Preset("Qwen2.5 0.5B Instruct Q4_K_M (~0.4 GB) – test rapido",
            "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf"),
        Preset("Llama 3.2 3B Instruct Q4_K_M (~2 GB)",
            "https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf"),
        Preset("Qwen2.5 7B Instruct Q4_K_M (~4.7 GB) – servono più telefoni",
            "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf"),
        Preset("Qwen2.5 14B Instruct Q4_K_M (~9 GB) – tanti telefoni",
            "https://huggingface.co/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf"),
    )

    fun dir(ctx: Context): File =
        (ctx.getExternalFilesDir("models") ?: File(ctx.filesDir, "models")).apply { mkdirs() }

    fun list(ctx: Context): List<File> =
        dir(ctx).listFiles { f -> f.name.endsWith(".gguf") }?.sortedBy { it.name } ?: emptyList()

    /**
     * Scarica con ripresa (header Range) in un file .part, rinominato a fine download.
     * [progress] riceve (byte scaricati, byte totali o -1). Bloccante: chiamare da un thread.
     */
    fun download(ctx: Context, url: String, cancelled: () -> Boolean, progress: (Long, Long) -> Unit): File {
        val name = url.substringAfterLast('/').substringBefore('?')
        require(name.endsWith(".gguf")) { "l'URL deve puntare a un file .gguf" }
        val target = File(dir(ctx), name)
        if (target.exists()) return target
        val part = File(dir(ctx), "$name.part")
        var have = if (part.exists()) part.length() else 0L

        var conn = URL(url).openConnection() as HttpURLConnection
        conn.instanceFollowRedirects = true
        conn.connectTimeout = 15000; conn.readTimeout = 30000
        if (have > 0) conn.setRequestProperty("Range", "bytes=$have-")
        val code = conn.responseCode
        if (code == 200 && have > 0) have = 0 // il server ignora Range: si riparte da capo
        if (code !in listOf(200, 206)) error("HTTP $code")
        val total = conn.getHeaderField("Content-Length")?.toLongOrNull()?.let { it + have } ?: -1

        RandomAccessFile(part, "rw").use { out ->
            out.setLength(have); out.seek(have)
            conn.inputStream.use { inp ->
                val buf = ByteArray(1 shl 16)
                var last = 0L
                while (true) {
                    if (cancelled()) error("download annullato")
                    val n = inp.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    have += n
                    if (have - last > (4 shl 20)) { progress(have, total); last = have }
                }
            }
        }
        conn.disconnect()
        progress(have, total)
        if (total > 0 && have < total) error("download incompleto")
        part.renameTo(target)
        return target
    }
}
