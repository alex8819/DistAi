package com.distai.app

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.util.concurrent.ConcurrentHashMap
import kotlin.concurrent.thread

/**
 * Battito UDP anti power-save Wi-Fi.
 *
 * Un telefono inattivo mette la radio in risparmio energetico e il router gli consegna i pacchetti in arrivo
 * solo al beacon successivo: misurati fino a 540 ms di ritardo (media 205 ms) su un Mate 10 Pro.
 * Con un pacchetto da 1 byte ogni 100 ms la latenza in entrata scende a ~5 ms costanti.
 *
 * Ogni nodo invia il battito ai propri pari sulla porta 50054; chi riceve un battito aggiunge il mittente
 * ai pari, quindi basta che il coordinatore contatti un worker perché il collegamento resti sveglio
 * in entrambe le direzioni.
 */
object Keepalive {
    private const val INTERVAL_MS = 100L
    /** un pari che non si sente da questo tempo viene dimenticato */
    private const val PEER_TTL_MS = 30_000L

    private val peers = ConcurrentHashMap<String, Long>()
    private val pinned = ConcurrentHashMap.newKeySet<String>()
    @Volatile private var socket: DatagramSocket? = null

    /** Aggiunge pari fissi (es. i worker scelti dal coordinatore). */
    fun pin(hosts: Collection<String>) {
        pinned.addAll(hosts.filter { it != "127.0.0.1" })
    }

    fun unpinAll() = pinned.clear()

    fun start() {
        if (socket != null) return
        val s = runCatching { DatagramSocket(null).apply { reuseAddress = true; bind(InetSocketAddress(Native.KEEPALIVE_PORT)) } }
            .getOrElse { NodeState.log("keepalive", "porta ${Native.KEEPALIVE_PORT} non disponibile: ${it.message}"); return }
        socket = s
        thread(name = "keepalive-rx", isDaemon = true) {
            val buf = ByteArray(16)
            val p = DatagramPacket(buf, buf.size)
            while (!s.isClosed) {
                runCatching { s.receive(p) }.getOrNull() ?: continue
                p.address?.hostAddress?.let { peers[it] = System.currentTimeMillis() }
            }
        }
        thread(name = "keepalive-tx", isDaemon = true) {
            val ping = byteArrayOf('k'.code.toByte())
            while (!s.isClosed) {
                val now = System.currentTimeMillis()
                peers.entries.removeIf { now - it.value > PEER_TTL_MS }
                for (h in pinned + peers.keys) {
                    runCatching { s.send(DatagramPacket(ping, 1, InetAddress.getByName(h), Native.KEEPALIVE_PORT)) }
                }
                Thread.sleep(INTERVAL_MS)
            }
        }
    }

    fun stop() {
        runCatching { socket?.close() }
        socket = null
        peers.clear(); pinned.clear()
    }
}
