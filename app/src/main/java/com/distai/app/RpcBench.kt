package com.distai.app

import java.io.DataInputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Mini-benchmark di un worker ggml-rpc (protocollo v8) in pochi secondi, senza modello (come tools/rpcbench.py e devbench.py):
 * - [run]: 4 matrici Q4_0 4096×4096 (~38 MB) riempite con MEMSET (in rete non viaggiano dati) e una catena di 16 prodotti
 *   matrice-vettore, il lavoro di un token: round-trip del canale e banda effettiva, che decide i tok/s in generazione.
 * - [compute]: prodotti matrice-matrice Q4_0 × 64 token, come la lettura di un prompt: GFLOPS.
 * Misurati 12/10/2026: PC i7-7700 16,7 GB/s e 209 GFLOPS, Note 10 14,6 e 62, Xiaomi Note 12 Pro 12,0 e 69, Pi 5 6,4 e 99.
 * Il worker serve un client alla volta: va usato prima di avviare il coordinatore.
 */
object RpcBench {
    data class Result(val rttMs: Double, val gbps: Double)

    private const val HELLO = 14
    private const val DEVICE_COUNT = 15
    private const val ALLOC = 0
    private const val GET_BASE = 3
    private const val FREE = 4
    private const val GET_TENSOR = 8
    private const val GRAPH_COMPUTE = 10
    private const val MEMSET = 17
    private const val Q4_0 = 2
    private const val F32 = 0
    private const val OP_MUL_MAT = 29
    /** GGML_TENSOR_FLAG_COMPUTE: senza, il backend CPU salta il nodo */
    private const val FLAG_COMPUTE = 16
    private const val TENSOR_SIZE = 296

    private const val K = 4096
    private const val MATS = 4
    private const val CHAIN = 16
    private const val QROW = K / 32 * 18
    private const val WBYTES = QROW.toLong() * K
    private const val VBYTES = K * 4L
    /** colonne del prodotto matrice-matrice (un prompt corto) e prodotti per grafo */
    private const val TOKENS = 64
    private const val MM_CHAIN = 4

    /** Connessione RPC aperta con HELLO verificato. */
    private class Conn(host: String, port: Int, timeoutMs: Int) : AutoCloseable {
        val s = Socket().apply { connect(InetSocketAddress(host, port), timeoutMs); soTimeout = timeoutMs; tcpNoDelay = true }
        val out = s.getOutputStream().buffered(1 shl 16)
        val inp = DataInputStream(s.getInputStream().buffered(1 shl 16))

        init {
            val hello = call(HELLO, ByteArray(24))
            require(hello[0].toInt() == 8) { "protocollo RPC ${hello[0]}.${hello[1]}" }
        }

        fun send(cmd: Int, payload: ByteArray) {
            out.write(cmd)
            out.write(buf(8) { putLong(payload.size.toLong()) })
            out.write(payload)
            out.flush()
        }

        fun call(cmd: Int, payload: ByteArray): ByteArray {
            send(cmd, payload)
            val n = java.lang.Long.reverseBytes(inp.readLong()).toInt()
            return ByteArray(n).also { inp.readFully(it) }
        }

        /** buffer sul worker allineato a 64 byte: (id, indirizzo base) */
        fun alloc(size: Long): Pair<Long, Long> {
            val buffer = le(call(ALLOC, buf(12) { putInt(0); putLong(size) })).long
            require(buffer != 0L) { "il worker non ha RAM per il benchmark" }
            return buffer to (le(call(GET_BASE, buf(8) { putLong(buffer) })).long + 63) / 64 * 64
        }

        /** tempo migliore (ms) di un grafo, sincronizzato leggendo 4 byte del risultato */
        fun timeGraph(graph: ByteArray, last: ByteArray, minRuns: Int, minMs: Double): Double {
            val get = last + buf(16) { putLong(0); putLong(4) }
            val times = ArrayList<Double>()
            while (times.size < minRuns || (times.sum() < minMs && times.size < 60)) {
                val t = System.nanoTime()
                send(GRAPH_COMPUTE, graph)
                call(GET_TENSOR, get) // risponde a calcolo finito
                times += (System.nanoTime() - t) / 1e6
            }
            return times.drop(1).min()
        }

        override fun close() = s.close()
    }

    fun run(host: String, port: Int, timeoutMs: Int = 5000): Result? = runCatching {
        Conn(host, port, timeoutMs).use { c ->
            val rtt = (1..5).minOf {
                val t = System.nanoTime(); c.call(DEVICE_COUNT, ByteArray(0)); (System.nanoTime() - t) / 1e6
            }
            val (buffer, base) = c.alloc(MATS * WBYTES + (CHAIN + 1) * VBYTES + 4096)
            val vbase = base + MATS * WBYTES
            val ws = (0 until MATS).map { tensor(1L + it, Q4_0, buffer, K, K, 18, QROW.toLong(), base + it * WBYTES) }
            fun vec(i: Int, src: List<Long> = emptyList()) =
                tensor(100L + i, F32, buffer, K, 1, 4, VBYTES, vbase + i * VBYTES, if (src.isEmpty()) 0 else OP_MUL_MAT, src)
            // pagine vere (non la pagina zero condivisa) e scale fp16 0x1111 valide
            ws.forEach { c.send(MEMSET, it + buf(17) { putLong(0); putLong(WBYTES); put(0x11) }) }
            c.send(MEMSET, vec(0) + buf(17) { putLong(0); putLong(VBYTES); put(0x11) })
            val nodes = (0 until CHAIN).map { vec(it + 1, listOf(1L + it % MATS, 100L + it)) }
            // almeno 6 grafi e 0,4 s di calcolo: la CPU sale di frequenza solo dopo un po'
            val best = c.timeGraph(graph(ws + listOf(vec(0)) + nodes, CHAIN), nodes.last(), 6, 400.0)
            c.send(FREE, buf(8) { putLong(buffer) })
            Result(rtt, CHAIN * WBYTES / maxOf(0.1, best - rtt) / 1e6)
        }
    }.onFailure { NodeState.log("bench", "$host:$port: ${it.message}") }.getOrNull()

    /** GFLOPS di prodotti matrice-matrice Q4_0 4096×4096 per F32 4096×64 (lettura del prompt); null se fallisce. */
    fun compute(host: String, port: Int, timeoutMs: Int = 15000): Double? = runCatching {
        Conn(host, port, timeoutMs).use { c ->
            val act = K * TOKENS * 4L
            val (buffer, base) = c.alloc(WBYTES + (MM_CHAIN + 1) * act + 4096)
            val w = tensor(1, Q4_0, buffer, K, K, 18, QROW.toLong(), base)
            val abase = base + WBYTES
            fun x(i: Int, src: List<Long> = emptyList()) =
                tensor(100L + i, F32, buffer, K, TOKENS, 4, K * 4L, abase + i * act, if (src.isEmpty()) 0 else OP_MUL_MAT, src)
            c.send(MEMSET, w + buf(17) { putLong(0); putLong(WBYTES); put(0x11) })
            c.send(MEMSET, x(0) + buf(17) { putLong(0); putLong(act); put(0x11) })
            val nodes = (0 until MM_CHAIN).map { x(it + 1, listOf(1L, 100L + it)) }
            val best = c.timeGraph(graph(listOf(w, x(0)) + nodes, MM_CHAIN), nodes.last(), 4, 1500.0)
            c.send(FREE, buf(8) { putLong(buffer) })
            MM_CHAIN * 2.0 * K * K * TOKENS / (best / 1000) / 1e9
        }
    }.onFailure { NodeState.log("bench", "$host:$port calcolo: ${it.message}") }.getOrNull()

    /** grafo con gli ultimi [n] tensori come nodi da calcolare (id 101..100+n) */
    private fun graph(tensors: List<ByteArray>, n: Int): ByteArray =
        ByteBuffer.allocate(16 + n * 8 + 4 + tensors.size * TENSOR_SIZE).order(ByteOrder.LITTLE_ENDIAN).apply {
            putInt(0); putLong(0); putInt(n)
            repeat(n) { putLong(101L + it) }
            putInt(tensors.size); tensors.forEach { put(it) }
        }.array()

    /** rpc_tensor 2D: [ne0]×[ne1] elementi, passi nb0/nb1 in byte */
    private fun tensor(id: Long, type: Int, buffer: Long, ne0: Int, ne1: Int, nb0: Long, nb1: Long, data: Long,
                       op: Int = 0, src: List<Long> = emptyList()) =
        buf(TENSOR_SIZE) {
            putLong(id); putInt(type); putLong(buffer)
            intArrayOf(ne0, ne1, 1, 1).forEach { putInt(it) }
            val nb2 = nb1 * ne1
            longArrayOf(nb0, nb1, nb2, nb2).forEach { putInt(it.toInt()) }
            putInt(op)
            repeat(16) { putInt(0) }                              // op_params
            putInt(if (op != 0) FLAG_COMPUTE else 0)
            repeat(10) { putLong(src.getOrElse(it) { 0L }) }
            putLong(0); putLong(0)                                // view_src, view_offs
            putLong(data)
            position(position() + 64)                             // name
            putInt(1)                                             // use_count
        }

    private fun buf(n: Int, f: ByteBuffer.() -> Unit): ByteArray =
        ByteBuffer.allocate(n).order(ByteOrder.LITTLE_ENDIAN).apply(f).array()

    private fun le(b: ByteArray) = ByteBuffer.wrap(b).order(ByteOrder.LITTLE_ENDIAN)
}
