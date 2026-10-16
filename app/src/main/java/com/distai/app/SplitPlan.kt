package com.distai.app

/**
 * Ripartizione dei layer tra coordinatore e nodi remoti, rispettando la RAM di ognuno.
 * llama.cpp mette i primi (n_layer - ngl) layer sulla CPU del coordinatore e gli ultimi ngl sui dispositivi RPC,
 * divisi tra loro secondo --tensor-split.
 *
 * Con la velocità dei nodi (RpcBench) la ripartizione è pesata: un token attraversa i nodi uno dopo l'altro,
 * quindi il tempo per token è la SOMMA dei tempi dei nodi e conviene riempire prima i più veloci fino alla loro RAM,
 * lasciando ai lenti solo ciò che non entra (misurato 3B Huawei+Pi: 18+18 → 1,84 tok/s, 28+8 → 2,33).
 * I nodi che restano senza layer escono dal piano: un salto di rete in meno per token.
 *
 * Se il modello non entra nella RAM del cluster il piano fallisce invece di mandare un telefono in OOM
 * (un Note 10 si è spento caricando da solo un 7B da 6,3 GB).
 */
sealed class SplitPlan {
    data class Ok(
        val layers: Int,
        val localLayers: Int,
        val remote: List<Node>,
        val remoteSplit: List<Int>,
        /** MB stimati per nodo, coordinatore per primo */
        val needMb: List<Long>,
        val budgetMb: List<Long>,
        /** tok/s stimati dal benchmark (0 se manca la velocità di qualche nodo) */
        val estTokS: Double = 0.0,
        /** giudizio su ogni nodo remoto: necessario, quanto aggiunge, o escluso e quanto costerebbe */
        val notes: List<String> = emptyList(),
    ) : SplitPlan() {
        val remoteLayers get() = layers - localLayers

        fun describe(coordName: String = "questo telefono"): String = buildString {
            append("piano: $layers layer · $coordName $localLayers (${needMb[0]}/${budgetMb[0]} MB)")
            remote.forEachIndexed { i, n -> append(" · ${n.name} ${remoteSplit[i]} (${needMb[i + 1]}/${budgetMb[i + 1]} MB)") }
            if (estTokS > 0) append(" · stima ${"%.1f".format(estTokS)} tok/s")
            notes.forEach { append("\n  • $it") }
        }
    }

    data class TooBig(val needMb: Long, val haveMb: Long) : SplitPlan() {
        fun describe() = "Il modello non entra: servono ≈${"%.1f".format(needMb / 1024.0)} GB, " +
            "disponibili ≈${"%.1f".format(haveMb / 1024.0)} GB. Aggiungi telefoni o usa un modello più piccolo."
    }

    companion object {
        /** margine oltre a MemAvailable (tarato: un 3B da 2,1 GB gira su Mate 10 Pro con 2,85 GB disponibili) */
        const val RESERVE_MB = 400L
        /** buffer di calcolo (attivazioni, grafo) per nodo */
        const val COMPUTE_MB = 200L
        /** stima se il nodo non ha comunicato la RAM libera (aggiunto a mano) */
        const val UNKNOWN_FREE_MB = 2000L
        /**
         * Frazione di MemAvailable davvero usabile da un telefono WORKER: Android vi conta anche la memoria che recupera
         * comprimendo in zram, e i pesi ricevuti via RPC (RAM anonima) ci finiscono. Misurato il 12/10/2026 sul Note 10:
         * 4,1 GB "liberi", con 3,4 GB di pesi 2,1 GB in zram e 2,19 tok/s invece di 3,98 (tools/plan.py).
         * Il coordinatore legge il modello in mmap (pagine di file, non compresse): per lui vale [ANDROID_COORD_FRAC].
         */
        const val ANDROID_RAM_FRAC = 0.5
        /**
         * Frazione di MemAvailable usabile da un telefono COORDINATORE (mmap). Il 12/10/2026 il Note 10 con 3,1 GB di
         * budget su 3,9 GB liberi (80%, 7B Q6_K) è stato chiuso da lmkd durante il caricamento; il 3B da 2,1 GB su
         * Mate 10 Pro con 2,85 GB disponibili (74%) andava. 60% per stare larghi.
         */
        const val ANDROID_COORD_FRAC = 0.6

        private fun usable(freeMb: Long, android: Boolean) = if (android) (freeMb * ANDROID_RAM_FRAC).toLong() else freeMb

        private const val MB = 1048576L

        /**
         * [localGbps] e [Node.gbps] sono le bande misurate da RpcBench: se mancano per qualche nodo
         * la ripartizione torna proporzionale alla RAM libera, come prima del benchmark.
         */
        fun make(
            info: Gguf.Info, ctx: Int, localFreeMb: Long, remote: List<Node>, localGbps: Double = 0.0,
            localAndroid: Boolean = false,
        ): SplitPlan {
            val localFree = if (localAndroid) (localFreeMb * ANDROID_COORD_FRAC).toLong() else localFreeMb
            val L = info.layers
            val perLayerMb = (info.avgLayerBytes + info.kvBytesPerLayer(ctx)) / MB + 1
            val otherMb = info.otherBytes / MB
            // RAM utilizzabile per i layer: il coordinatore tiene anche embedding e output
            val budgets = listOf(localFree - RESERVE_MB - COMPUTE_MB - otherMb) +
                remote.map { usable(if (it.freeMb > 0) it.freeMb else UNKNOWN_FREE_MB, it.android) - RESERVE_MB - COMPUTE_MB }
            val caps = budgets.map { maxOf(0L, it / perLayerMb).toInt() }
            // il coordinatore tiene comunque embedding e output: se non ci stanno, leggerebbe dal disco a ogni token
            if (budgets[0] < 0) return TooBig(otherMb + RESERVE_MB + COMPUTE_MB, localFree)
            if (L == 0 || caps.sum() < L) {
                val need = L * perLayerMb + otherMb + (remote.size + 1) * (RESERVE_MB + COMPUTE_MB)
                val have = localFree +
                    remote.sumOf { usable(if (it.freeMb > 0) it.freeMb else UNKNOWN_FREE_MB, it.android) }
                return TooBig(need, have)
            }

            val speeds = listOf(localGbps) + remote.map { it.gbps }
            val weighted = speeds.all { it > 0 }
            var alloc = MutableList(caps.size) { 0 }
            var notes = emptyList<String>()
            if (weighted) {
                val choice = bestSubset(info, L, caps, speeds, remote)
                alloc = choice.first
                notes = choice.second
            } else {
                // proporzionale al budget, poi limitato alla capacità; il resto va a chi ha più spazio
                val pos = budgets.map { maxOf(0L, it) }
                val tot = pos.sum().toDouble()
                pos.indices.forEach { alloc[it] = minOf(caps[it], (pos[it] * L / tot).toInt()) }
                while (alloc.sum() < L) {
                    val i = alloc.indices.filter { alloc[it] < caps[it] }.maxBy { caps[it] - alloc[it] }
                    alloc[i]++
                }
            }
            val keep = remote.indices.filter { alloc[it + 1] > 0 }
            val need = alloc.mapIndexed { i, a -> a * perLayerMb + if (i == 0) otherMb else 0 }
            // stima: byte letti per token / banda di ogni nodo + un round-trip per ogni nodo remoto usato
            val est = if (!weighted) 0.0 else 1000 / tokenMs(info, alloc, speeds, remote)
            return Ok(
                L, alloc[0], keep.map { remote[it] }, keep.map { alloc[it + 1] },
                listOf(need[0]) + keep.map { need[it + 1] },
                listOf(budgets[0] + otherMb) + keep.map { budgets[it + 1] },
                est, notes,
            )
        }

        /**
         * Costo in più per ogni nodo remoto usato, oltre al round-trip misurato da RpcBench: sincronizzazione del grafo,
         * risveglio dei core, relay. Tarato sulle misure del 12/10/2026 con il 0.5B: PC da solo 34 tok/s, + Samsung via USB
         * 26 tok/s (+9 ms per un salto), su 5 dispositivi 7,6 tok/s (circa +24 ms per salto).
         */
        const val HOP_MS = 10.0
        /** oltre questa perdita stimata un nodo che non serve per la RAM è segnalato come svantaggio esagerato */
        const val TOO_SLOW = 0.30
        /** con più nodi remoti di così non provo tutti i sottoinsiemi (2^n): solo il riempimento dei più veloci */
        private const val MAX_SUBSET_NODES = 12

        /** ms per token: byte letti / banda di ogni nodo + round-trip e costo fisso di ogni nodo remoto con layer. */
        private fun tokenMs(info: Gguf.Info, alloc: List<Int>, speeds: List<Double>, remote: List<Node>): Double {
            val layerGb = info.avgLayerBytes / 1e9
            return alloc.indices.sumOf { alloc[it] * layerGb / speeds[it] * 1000 } + info.otherBytes / 1e9 / speeds[0] * 1000 +
                remote.indices.filter { alloc[it + 1] > 0 }.sumOf { remote[it].rttMs + HOP_MS }
        }

        /**
         * Layer ai più veloci per primi fino alla loro RAM, usando solo i nodi di [use] (indici di caps; 0 = coordinatore).
         * [force]: nodo che riceve comunque la sua quota di RAM, per stimare quanto costerebbe usarlo. null se non entra.
         */
        private fun fill(L: Int, caps: List<Int>, speeds: List<Double>, use: List<Int>, force: Int? = null): MutableList<Int>? {
            if (use.sumOf { caps[it] } < L) return null
            val alloc = MutableList(caps.size) { 0 }
            var left = L
            if (force != null) {
                alloc[force] = minOf(caps[force], maxOf(1, L * caps[force] / use.sumOf { caps[it] }))
                left -= alloc[force]
            }
            for (i in use.filter { it != force }.sortedByDescending { speeds[it] }) {
                alloc[i] = minOf(caps[i], left); left -= alloc[i]
            }
            return alloc
        }

        /**
         * Il sottoinsieme di nodi remoti con più tok/s stimati (un nodo in più costa un salto di rete per token: se la RAM
         * degli altri basta e lui è lento, resta fuori) e una nota per ogni nodo: necessario per la RAM, quanto aggiunge,
         * o quanto costerebbe usarlo comunque.
         */
        private fun bestSubset(info: Gguf.Info, L: Int, caps: List<Int>, speeds: List<Double>, remote: List<Node>): Pair<MutableList<Int>, List<String>> {
            val cand = remote.indices.map { it + 1 }.filter { caps[it] > 0 }
            val ms = { a: List<Int> -> tokenMs(info, a, speeds, remote) }
            if (cand.size > MAX_SUBSET_NODES) return fill(L, caps, speeds, listOf(0) + cand)!! to emptyList()
            // tutti i sottoinsiemi che entrano in RAM, con il tempo per token del loro riempimento migliore
            val plans = (0 until (1 shl cand.size)).mapNotNull { mask ->
                val use = listOf(0) + cand.filterIndexed { b, _ -> mask shr b and 1 == 1 }
                fill(L, caps, speeds, use)?.let { a -> Triple(use, a, ms(a)) }
            }
            val best = plans.minBy { it.third }
            val notes = cand.map { i ->
                val n = remote[i - 1]
                val without = plans.filter { i !in it.first }.minByOrNull { it.third }
                when {
                    best.second[i] > 0 && without == null -> "${n.name}: necessario per la RAM"
                    best.second[i] > 0 -> "${n.name}: conviene, +${"%.0f".format((without!!.third / best.third - 1) * 100)}%"
                    else -> {
                        val forced = fill(L, caps, speeds, (best.first + i).distinct(), force = i)
                        val loss = forced?.let { 1 - best.third / ms(it) } ?: 0.0
                        "${n.name}: escluso, non serve" + if (forced == null) "" else
                            "; usarlo per la sua RAM costerebbe ${"%.0f".format(loss * 100)}%" +
                                if (loss > TOO_SLOW) " ⚠ svantaggio esagerato" else ""
                    }
                }
            } + remote.indices.filter { caps[it + 1] == 0 }.map { "${remote[it].name}: escluso, RAM libera insufficiente" }
            return best.second to notes
        }
    }
}
