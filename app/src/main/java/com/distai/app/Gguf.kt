package com.distai.app

import java.io.BufferedInputStream
import java.io.DataInputStream
import java.io.File
import java.io.FileInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** Lettore minimale dei metadati GGUF: quanto basta per pianificare lo split dei layer e la RAM. */
object Gguf {
    data class Info(
        val arch: String,
        val layers: Int,
        val sizeBytes: Long,
        /** byte dei pesi di ciascun layer (tensori blk.N.*) */
        val layerBytes: LongArray,
        /** embedding, output e norme finali: restano sul coordinatore */
        val otherBytes: Long,
        /** dimensione K+V per token per layer (n_kv_heads * (key_len + value_len)) */
        val kvDimPerLayer: Long,
        /** tokenizer e numero di token: il modello predittivo deve avere lo stesso vocabolario del modello grande */
        val tokenizer: String = "",
        val vocab: Long = 0,
    ) {
        val avgLayerBytes get() = if (layers > 0) layerBytes.sum() / layers else 0L
        /** KV cache f16 di un layer per un contesto di [ctx] token */
        fun kvBytesPerLayer(ctx: Int) = ctx.toLong() * kvDimPerLayer * 2
    }

    /** [totalBytes]: dimensione del modello intero quando [file] è solo la sua intestazione (vedi Cluster.remoteInfo). */
    fun read(file: File, totalBytes: Long = file.length()): Info? = runCatching {
        DataInputStream(BufferedInputStream(FileInputStream(file), 1 shl 16)).use { inp ->
            val r = Reader(inp)
            require(r.u32() == 0x46554747L) { "non è un file GGUF" } // "GGUF" little-endian
            r.u32() // versione
            val nTensors = r.u64()
            val kvCount = r.u64()
            var arch = ""
            var tokenizer = ""
            var vocab = 0L
            val ints = HashMap<String, Long>()
            for (i in 0 until kvCount) {
                val key = r.str()
                val type = r.u32().toInt()
                when {
                    key == "general.architecture" && type == T_STRING -> arch = r.str()
                    key == "tokenizer.ggml.model" && type == T_STRING -> tokenizer = r.str()
                    key == "tokenizer.ggml.tokens" && type == T_ARRAY -> vocab = r.skipArray()
                    type in INT_TYPES -> ints[key] = r.int(type)
                    else -> r.skip(type)
                }
            }
            // descrittori dei tensori: nome, dimensioni, tipo, offset nella sezione dati
            val names = ArrayList<String>()
            val offsets = ArrayList<Long>()
            for (i in 0 until nTensors) {
                names += r.str()
                val nd = r.u32().toInt()
                repeat(nd) { r.u64() }
                r.u32()
                offsets += r.u64()
            }
            val align = ints["general.alignment"] ?: 32L
            val dataStart = (r.pos + align - 1) / align * align
            val dataLen = totalBytes - dataStart

            val layers = (ints["$arch.block_count"] ?: 0L).toInt()
            val layerBytes = LongArray(layers)
            var other = 0L
            val order = offsets.indices.sortedBy { offsets[it] }
            for ((k, idx) in order.withIndex()) {
                val end = if (k + 1 < order.size) offsets[order[k + 1]] else dataLen
                val size = end - offsets[idx]
                val n = names[idx]
                val blk = if (n.startsWith("blk.")) n.substring(4).substringBefore('.').toIntOrNull() else null
                if (blk != null && blk in 0 until layers) layerBytes[blk] += size else other += size
            }

            val emb = ints["$arch.embedding_length"] ?: 0L
            val heads = ints["$arch.attention.head_count"] ?: 1L
            val kvHeads = ints["$arch.attention.head_count_kv"] ?: heads
            val headDim = if (heads > 0) emb / heads else 0L
            val kLen = ints["$arch.attention.key_length"] ?: headDim
            val vLen = ints["$arch.attention.value_length"] ?: headDim
            Info(arch, layers, totalBytes, layerBytes, other, kvHeads * (kLen + vLen), tokenizer, vocab)
        }
    }.getOrNull()

    private const val T_STRING = 8
    private const val T_ARRAY = 9
    private val INT_TYPES = setOf(0, 1, 2, 3, 4, 5, 10, 11)

    private class Reader(val inp: DataInputStream) {
        var pos = 0L
            private set
        private val b = ByteArray(8)
        private fun le(n: Int): ByteBuffer {
            inp.readFully(b, 0, n); pos += n
            return ByteBuffer.wrap(b, 0, n).order(ByteOrder.LITTLE_ENDIAN)
        }
        fun u32(): Long = le(4).int.toLong() and 0xFFFFFFFFL
        fun u64(): Long = le(8).long
        fun str(): String {
            val n = u64().toInt()
            val a = ByteArray(n); inp.readFully(a); pos += n
            return String(a)
        }

        // tipi GGUF: 0 u8, 1 i8, 2 u16, 3 i16, 4 u32, 5 i32, 6 f32, 7 bool, 8 string, 9 array, 10 u64, 11 i64, 12 f64
        fun int(type: Int): Long = when (type) {
            0 -> le(1).get().toLong() and 0xFF
            1 -> le(1).get().toLong()
            2 -> le(2).short.toLong() and 0xFFFF
            3 -> le(2).short.toLong()
            4 -> u32()
            5 -> le(4).int.toLong()
            else -> u64()
        }

        // InputStream.skipNBytes non esiste sulle API Android vecchie
        fun skipN(n: Long) {
            var left = n
            while (left > 0) {
                val k = inp.skip(left)
                if (k <= 0) { inp.readByte(); left-- } else left -= k
            }
            pos += n
        }

        /** salta un array e ne restituisce la lunghezza */
        fun skipArray(): Long {
            val et = u32().toInt()
            val n = u64()
            val fixed = when (et) { 0, 1, 7 -> 1L; 2, 3 -> 2L; 4, 5, 6 -> 4L; 10, 11, 12 -> 8L; else -> 0L }
            if (fixed > 0) skipN(n * fixed) else for (i in 0 until n) skip(et)
            return n
        }

        fun skip(type: Int) {
            when (type) {
                0, 1, 7 -> skipN(1)
                2, 3 -> skipN(2)
                4, 5, 6 -> skipN(4)
                10, 11, 12 -> skipN(8)
                T_STRING -> skipN(u64())
                T_ARRAY -> skipArray()
                else -> error("tipo GGUF sconosciuto $type")
            }
        }
    }
}
