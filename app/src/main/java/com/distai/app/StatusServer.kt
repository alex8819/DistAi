package com.distai.app

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.InputStream
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.ServerSocket
import java.net.Socket
import java.net.URL
import java.net.URLDecoder
import kotlin.concurrent.thread

/**
 * Pannello di controllo HTTP del nodo (porta 50053), usato dal coordinatore:
 *  - GET  /status          stato in JSON: RAM libera reale, CPU, batteria, temperatura, ruoli, modelli, replica
 *  - GET  /files/NOME.gguf modello locale (con Range), per copiarlo agli altri telefoni via LAN
 *  - POST /replica/start   {"model":"x.gguf","source":"http://…/files/x.gguf","ctx":4096} → scarica se manca e avvia la replica
 *  - POST /replica/stop
 *  - POST /coord/start     {"model","source","rpc","ngl","ts","ctx","draft","draftSource","draftMax"} → questo telefono
 *                          diventa il coordinatore scelto da un altro (scarica i modelli che mancano)
 *  - POST /coord/stop
 * Come l'RPC di llama.cpp non ha autenticazione: solo per LAN private.
 */
class StatusServer(private val ctx: Context) {
    private var socket: ServerSocket? = null

    fun start() {
        if (socket != null) return
        val s = runCatching { ServerSocket(Native.STATUS_PORT) }.getOrElse {
            NodeState.log("status", "porta ${Native.STATUS_PORT} non disponibile: ${it.message}")
            return
        }
        socket = s
        thread(name = "status-http", isDaemon = true) {
            while (!s.isClosed) {
                val c = runCatching { s.accept() }.getOrNull() ?: break
                thread(name = "status-conn", isDaemon = true) { runCatching { handle(c) }; runCatching { c.close() } }
            }
        }
    }

    fun stop() {
        runCatching { socket?.close() }
        socket = null
    }

    private fun handle(c: Socket) {
        c.soTimeout = 15000
        val inp = c.getInputStream()
        val req = readHead(inp) ?: return
        val out = c.getOutputStream()
        val path = req.path.substringBefore('?')
        when {
            req.method == "GET" && path == "/status" -> json(out, status(ctx))
            req.method == "GET" && path.startsWith("/files/") -> serveFile(out, path.removePrefix("/files/"), req.headers["range"])
            req.method == "POST" && path == "/replica/start" -> {
                val body = JSONObject(String(readBody(inp, req.contentLength)).ifEmpty { "{}" })
                startReplica(body)
                json(out, JSONObject().put("ok", true).put("state", NodeState.replicaState), 202)
            }
            req.method == "POST" && path == "/replica/stop" -> {
                NodeService.send(ctx, NodeService.ACTION_REPLICA_STOP)
                json(out, JSONObject().put("ok", true))
            }
            req.method == "POST" && path == "/coord/start" -> {
                startCoordinator(JSONObject(String(readBody(inp, req.contentLength)).ifEmpty { "{}" }))
                json(out, JSONObject().put("ok", true), 202)
            }
            req.method == "POST" && path == "/coord/stop" -> {
                NodeService.send(ctx, NodeService.ACTION_COORD_STOP)
                json(out, JSONObject().put("ok", true))
            }
            else -> json(out, JSONObject().put("error", "not found"), 404)
        }
    }

    /** Scarica il modello dal coordinatore se manca, poi avvia llama-server locale come replica. */
    private fun startReplica(body: JSONObject) {
        val name = File(body.optString("model")).name
        val source = body.optString("source")
        val ctxSize = body.optInt("ctx", 4096)
        if (!name.endsWith(".gguf")) { NodeState.replicaState = "errore: modello non valido"; return }
        if (NodeState.replicaRunning || NodeState.replicaState.startsWith("download")) return
        thread(name = "replica-prepare") {
            val local = File(ModelStore.dir(ctx), name)
            val model = if (local.exists()) local else runCatching {
                require(source.isNotEmpty()) { "modello assente e nessuna sorgente" }
                NodeState.replicaState = "download 0%"
                ModelStore.download(ctx, source, { false }) { done, total ->
                    NodeState.replicaState = if (total > 0) "download ${done * 100 / total}%" else "download ${done shr 20} MB"
                }
            }.getOrElse {
                NodeState.replicaState = "errore download: ${it.message}"
                NodeState.log("replica", NodeState.replicaState)
                return@thread
            }
            NodeService.send(ctx, NodeService.ACTION_REPLICA_START) {
                putExtra(NodeService.EXTRA_MODEL, model.path)
                putExtra(NodeService.EXTRA_CTX, ctxSize)
            }
        }
    }

    /** Il modello [name] in locale, altrimenti scaricato da [source] (il telefono che ha scelto questo coordinatore). */
    private fun ensureModel(name: String, source: String, onProgress: (String) -> Unit): File {
        require(name.endsWith(".gguf")) { "modello non valido" }
        val local = File(ModelStore.dir(ctx), File(name).name)
        if (local.exists()) return local
        require(source.isNotEmpty()) { "modello assente e nessuna sorgente" }
        return ModelStore.download(ctx, source, { false }) { done, total ->
            onProgress(if (total > 0) "download $name ${done * 100 / total}%" else "download $name ${done shr 20} MB")
        }
    }

    /** Coordinatore delegato: i parametri (nodi, split) li ha calcolati il telefono che ha avviato il cluster. */
    private fun startCoordinator(body: JSONObject) {
        if (NodeState.coordinatorRunning) return
        thread(name = "coord-prepare") {
            runCatching {
                val model = ensureModel(body.optString("model"), body.optString("source")) { NodeState.coordinatorInfo = it; NodeState.notifyChanged() }
                val draft = body.optString("draft").takeIf { it.isNotEmpty() }?.let {
                    ensureModel(it, body.optString("draftSource")) { s -> NodeState.coordinatorInfo = s }
                }
                NodeState.log("coord", "coordinatore delegato: ${model.name}${draft?.let { " + bozza ${it.name}" } ?: ""}")
                NodeService.send(ctx, NodeService.ACTION_COORD_START) {
                    putExtra(NodeService.EXTRA_MODEL, model.path)
                    putExtra(NodeService.EXTRA_RPC, body.optString("rpc"))
                    putExtra(NodeService.EXTRA_NGL, body.optInt("ngl"))
                    putExtra(NodeService.EXTRA_TS, body.optString("ts"))
                    putExtra(NodeService.EXTRA_CTX, body.optInt("ctx", 4096))
                    draft?.let { putExtra(NodeService.EXTRA_DRAFT, it.path); putExtra(NodeService.EXTRA_DRAFT_MAX, body.optInt("draftMax", 4)) }
                    putExtra(NodeService.EXTRA_EXPERTS, body.optInt("experts", 0))
                }
            }.onFailure { NodeState.log("coord", "coordinatore delegato: errore ${it.message}") }
        }
    }

    private fun serveFile(out: OutputStream, rawName: String, range: String?) {
        val name = File(URLDecoder.decode(rawName, "UTF-8")).name
        val f = File(ModelStore.dir(ctx), name)
        if (!name.endsWith(".gguf") || !f.isFile) return json(out, JSONObject().put("error", "not found"), 404)
        val len = f.length()
        val from = range?.removePrefix("bytes=")?.substringBefore('-')?.toLongOrNull()?.coerceIn(0, len) ?: 0L
        val head = if (from > 0) "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes $from-${len - 1}/$len\r\n" else "HTTP/1.1 200 OK\r\n"
        out.write((head + "Content-Type: application/octet-stream\r\nContent-Length: ${len - from}\r\nConnection: close\r\n\r\n").toByteArray())
        f.inputStream().use { fin ->
            var skip = from
            while (skip > 0) skip -= fin.skip(skip).coerceAtLeast(1)
            fin.copyTo(out, 1 shl 16)
        }
        out.flush()
    }

    private class Head(val method: String, val path: String, val headers: Map<String, String>) {
        val contentLength get() = headers["content-length"]?.toIntOrNull() ?: 0
    }

    private fun readHead(inp: InputStream): Head? {
        val lines = ArrayList<String>()
        val sb = StringBuilder()
        while (true) {
            val b = inp.read()
            if (b < 0) return null
            if (b == '\n'.code) {
                val line = sb.toString().trimEnd('\r'); sb.clear()
                if (line.isEmpty()) break
                lines += line
                if (lines.size > 100) return null
            } else if (sb.length < 8192) sb.append(b.toChar())
        }
        val first = lines.firstOrNull()?.split(' ') ?: return null
        if (first.size < 2) return null
        val headers = lines.drop(1).associate { it.substringBefore(':').trim().lowercase() to it.substringAfter(':').trim() }
        return Head(first[0], first[1], headers)
    }

    private fun readBody(inp: InputStream, n: Int): ByteArray {
        val a = ByteArray(n.coerceIn(0, 1 shl 20))
        var off = 0
        while (off < a.size) { val k = inp.read(a, off, a.size - off); if (k < 0) break; off += k }
        return a
    }

    private fun json(out: OutputStream, j: JSONObject, code: Int = 200) {
        val body = j.toString().toByteArray()
        val reason = when (code) { 200 -> "OK"; 202 -> "Accepted"; else -> "Not Found" }
        out.write(("HTTP/1.1 $code $reason\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\n" +
            "Content-Length: ${body.size}\r\nConnection: close\r\n\r\n").toByteArray())
        out.write(body)
        out.flush()
    }

    companion object {
        fun status(ctx: Context): JSONObject {
            WorkerStats.sample()
            val (total, free) = Native.memInfo(ctx)
            val bat = ctx.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            val level = bat?.let { 100 * it.getIntExtra(BatteryManager.EXTRA_LEVEL, 0) / maxOf(1, it.getIntExtra(BatteryManager.EXTRA_SCALE, 100)) }
            return JSONObject()
                .put("dev", Native.deviceName())
                .put("ram", total).put("free", free)
                .put("cores", Native.cores()).put("bigCores", Native.bigCores()).put("variant", Native.variant)
                .put("battery", level ?: -1)
                .put("charging", (bat?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0) != 0)
                .put("tempC", (bat?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) ?: 0) / 10.0)
                .put("worker", NodeState.workerRunning)
                .put("coordinator", NodeState.coordinatorRunning)
                .put("replica", NodeState.replicaRunning)
                .put("replicaReady", NodeState.replicaReady)
                .put("replicaState", NodeState.replicaState)
                .put("models", JSONArray(ModelStore.list(ctx).map { it.name }))
                .put("canCoord", true).put("apiPort", Native.API_PORT)
                .put("apiReady", NodeState.apiReady)
                .put("workerClients", WorkerStats.clients).put("workerMb", WorkerStats.rssMb).put("workerCpu", WorkerStats.cpuPct)
                .put("tokS", if (NodeState.apiReady) tokS("127.0.0.1", Native.API_PORT) ?: 0.0 else 0.0)
                .put("coordState", NodeState.coordinatorInfo)
                .put("bench", SelfBench.result ?: JSONObject())
        }

        /**
         * Velocità di generazione media del coordinatore in tok/s, dalle metriche di llama-server (--metrics);
         * null se non risponde o non ha ancora generato nulla.
         */
        fun tokS(host: String, port: Int, timeoutMs: Int = 1000): Double? = runCatching {
            val c = URL("http://$host:$port/metrics").openConnection() as HttpURLConnection
            c.connectTimeout = timeoutMs; c.readTimeout = timeoutMs
            c.inputStream.bufferedReader().useLines { lines ->
                lines.firstOrNull { it.startsWith("llamacpp:predicted_tokens_seconds ") }
                    ?.substringAfter(' ')?.toDoubleOrNull()?.takeIf { it > 0 }
            }
        }.getOrNull()

        /** Legge lo stato di un nodo remoto (da chiamare fuori dal main thread). */
        fun fetch(host: String, timeoutMs: Int = 1500, port: Int = Native.STATUS_PORT): JSONObject? = runCatching {
            val c = URL("http://$host:$port/status").openConnection() as HttpURLConnection
            c.connectTimeout = timeoutMs; c.readTimeout = timeoutMs
            c.inputStream.bufferedReader().use { JSONObject(it.readText()) }
        }.getOrNull()

        /** POST JSON al pannello di controllo di un nodo remoto (fuori dal main thread). */
        fun post(host: String, path: String, body: JSONObject = JSONObject(), timeoutMs: Int = 3000,
                 port: Int = Native.STATUS_PORT): Boolean = runCatching {
            val c = URL("http://$host:$port$path").openConnection() as HttpURLConnection
            c.requestMethod = "POST"; c.doOutput = true
            c.connectTimeout = timeoutMs; c.readTimeout = timeoutMs
            c.setRequestProperty("Content-Type", "application/json")
            c.outputStream.use { it.write(body.toString().toByteArray()) }
            c.responseCode in 200..299
        }.getOrDefault(false)
    }
}
