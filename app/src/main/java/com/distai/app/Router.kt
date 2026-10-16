package com.distai.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.InputStream
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URL
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

/**
 * Modalità "repliche": ogni telefono esegue l'intero modello (llama-server su :8081) e questo router,
 * sulla porta pubblica 8080, smista le richieste OpenAI-compatibili:
 *  - affinità di sessione: lo stesso client torna sullo stesso telefono, così riusa la cache del prompt;
 *  - altrimenti il telefono sano con meno richieste in corso.
 * Con N telefoni si servono N utenti in parallelo, ognuno alla velocità di un telefono singolo.
 * Con un solo backend fa da ponte verso un coordinatore delegato (es. il PC su :8090): l'API resta su questo telefono.
 *
 * Robustezza: salute controllata ogni secondo; dopo [FAILS] controlli falliti di fila il backend è dato per perso
 * e le sue richieste in corso vengono chiuse subito (prima restavano appese fino a 200 s). Se il backend cade prima
 * di aver mandato un byte al client, la richiesta riparte su un altro backend sano.
 */
class Router(backendEndpoints: List<String>) {
    companion object {
        /** slot presunti finché la replica non dichiara i suoi (`total_slots` di /props); `-np` in NodeService.startReplica */
        const val DEFAULT_SLOTS = 2
        private const val FAILS = 3
        /** limite di sicurezza senza byte dal backend; il controllo di salute di solito interviene molto prima */
        private const val IDLE_MS = 180_000
    }

    /** [ep] è "host" (porta delle repliche) oppure "host:porta" */
    private class Backend(ep: String) {
        val host = ep.substringBefore(':')
        val port = ep.substringAfter(':', "").toIntOrNull() ?: Native.REPLICA_PORT
        val name = "$host:$port"
        @Volatile var healthy = false
        /** richieste contemporanee che la replica serve bene: 4 con dotprod, 1 sul v8 (letto da /props) */
        @Volatile var slots = DEFAULT_SLOTS
        var fails = 0
        val inflight = AtomicInteger()
        val served = AtomicInteger()
        val retried = AtomicInteger()
        /** connessioni aperte verso il backend: chiuse se il backend sparisce, così il client non resta appeso */
        val open: MutableSet<Socket> = ConcurrentHashMap.newKeySet()
    }

    private val backends = backendEndpoints.map { Backend(it) }
    private val sticky = ConcurrentHashMap<String, Backend>()
    private var socket: ServerSocket? = null

    val healthyCount get() = backends.count { it.healthy }

    fun start(): Boolean {
        val s = runCatching { ServerSocket().apply { reuseAddress = true; bind(InetSocketAddress(Native.API_PORT)) } }
            .getOrElse { NodeState.log("router", "porta ${Native.API_PORT} occupata: ${it.message}"); return false }
        socket = s
        NodeState.log("router", "backend: ${backends.joinToString { it.name }}")
        for (b in backends) thread(name = "router-health", isDaemon = true) {
            while (!s.isClosed) {
                val ok = runCatching {
                    val c = URL("http://${b.name}/health").openConnection() as HttpURLConnection
                    c.connectTimeout = 1000; c.readTimeout = 1500
                    c.responseCode == 200
                }.getOrDefault(false)
                if (ok) runCatching {
                    val c = URL("http://${b.name}/props").openConnection() as HttpURLConnection
                    c.connectTimeout = 1000; c.readTimeout = 1500
                    JSONObject(c.inputStream.bufferedReader().readText()).optInt("total_slots", 0).takeIf { it > 0 }?.let { b.slots = it }
                }
                b.fails = if (ok) 0 else b.fails + 1
                val now = if (ok) true else if (b.fails >= FAILS || !b.healthy) false else b.healthy
                if (now != b.healthy) {
                    b.healthy = now
                    NodeState.log("router", "${b.name} ${if (now) "pronto" else "perso: chiudo ${b.open.size} richieste in corso"}")
                    if (!now) b.open.forEach { runCatching { it.close() } }
                    val ready = healthyCount > 0
                    if (NodeState.apiReady != ready) NodeState.apiReady = ready
                    NodeState.notifyChanged()
                }
                Thread.sleep(1000)
            }
        }
        thread(name = "router-accept", isDaemon = true) {
            while (!s.isClosed) {
                val c = runCatching { s.accept() }.getOrNull() ?: break
                thread(name = "router-conn", isDaemon = true) { runCatching { handle(c) }; runCatching { c.close() } }
            }
        }
        return true
    }

    fun stop() {
        runCatching { socket?.close() }
        socket = null
        backends.forEach { b -> b.open.forEach { runCatching { it.close() } } }
    }

    private fun pick(client: String, skip: Set<Backend>): Backend? {
        // affinità solo se la replica ha ancora uno slot libero (-np): altrimenti le richieste parallele
        // dello stesso client (agenti, risposte divise in rami) finirebbero in coda su un solo telefono
        sticky[client]?.takeIf { it.healthy && it !in skip && it.inflight.get() < it.slots }?.let { return it }
        val b = backends.filter { it.healthy && it !in skip }.minByOrNull { it.inflight.get() } ?: return null
        sticky[client] = b
        return b
    }

    private fun handle(c: Socket) {
        c.soTimeout = 0
        val inp = c.getInputStream()
        val out = c.getOutputStream()
        val head = readHead(inp) ?: return
        val requestLine = head.first()
        val path = requestLine.split(' ').getOrNull(1).orEmpty()

        // risposte del router stesso
        if (path == "/health") {
            return if (healthyCount > 0) reply(out, 200, JSONObject().put("status", "ok").put("replicas", healthyCount))
            else reply(out, 503, JSONObject().put("status", "no replicas"))
        }
        if (path == "/router/status") return reply(out, 200, status())

        val length = head.firstOrNull { it.startsWith("content-length:", true) }?.substringAfter(':')?.trim()?.toIntOrNull() ?: 0
        val body = ByteArray(length).also { var off = 0; while (off < length) { val k = inp.read(it, off, length - off); if (k < 0) break; off += k } }
        val client = c.inetAddress.hostAddress ?: "?"
        val tried = HashSet<Backend>()
        while (true) {
            val b = pick(client, tried) ?: return reply(out, 503, JSONObject().put("error", "nessun backend pronto"))
            tried += b
            if (forward(b, requestLine, head, body, out)) return
            // il backend è caduto prima di rispondere: riprovo su un altro
            b.retried.incrementAndGet()
            NodeState.log("router", "${b.name} non ha risposto: nuovo tentativo su un altro backend")
        }
    }

    /** true se la richiesta è conclusa (anche con errore a metà risposta); false se si può riprovare altrove. */
    private fun forward(b: Backend, requestLine: String, head: List<String>, body: ByteArray, out: OutputStream): Boolean {
        var sent = false
        b.inflight.incrementAndGet()
        val up = Socket()
        b.open += up
        try {
            up.connect(InetSocketAddress(b.host, b.port), 3000)
            up.soTimeout = IDLE_MS
            val fwd = StringBuilder(requestLine).append("\r\n")
            head.drop(1).filterNot { it.startsWith("connection:", true) || it.startsWith("host:", true) }
                .forEach { fwd.append(it).append("\r\n") }
            fwd.append("Host: ${b.name}\r\nConnection: close\r\n\r\n")
            up.getOutputStream().apply { write(fwd.toString().toByteArray()); write(body); flush() }
            // risposta in streaming (SSE) passata così com'è
            val upIn = up.getInputStream()
            val buf = ByteArray(8192)
            while (true) {
                val n = upIn.read(buf)
                if (n < 0) break
                out.write(buf, 0, n); out.flush()
                sent = true
            }
            b.served.incrementAndGet()
            return true
        } catch (e: Exception) {
            if (!sent) b.fails = FAILS - 1 // un errore vero vale quasi come un controllo fallito: il prossimo lo esclude
            return sent
        } finally {
            b.open -= up
            runCatching { up.close() }
            b.inflight.decrementAndGet()
        }
    }

    fun status(): JSONObject = JSONObject().put("replicas", JSONArray(backends.map {
        JSONObject().put("host", it.name).put("healthy", it.healthy).put("inflight", it.inflight.get())
            .put("served", it.served.get()).put("retried", it.retried.get())
    }))

    private fun readHead(inp: InputStream): List<String>? {
        val lines = ArrayList<String>()
        val sb = StringBuilder()
        while (true) {
            val b = inp.read()
            if (b < 0) return null
            if (b == '\n'.code) {
                val line = sb.toString().trimEnd('\r'); sb.clear()
                if (line.isEmpty()) return lines.takeIf { it.isNotEmpty() }
                lines += line
                if (lines.size > 100) return null
            } else if (sb.length < 8192) sb.append(b.toChar())
        }
    }

    private fun reply(out: OutputStream, code: Int, j: JSONObject) {
        val body = j.toString().toByteArray()
        out.write(("HTTP/1.1 $code ${if (code == 200) "OK" else "Service Unavailable"}\r\nContent-Type: application/json\r\n" +
            "Access-Control-Allow-Origin: *\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n").toByteArray())
        out.write(body); out.flush()
    }
}
