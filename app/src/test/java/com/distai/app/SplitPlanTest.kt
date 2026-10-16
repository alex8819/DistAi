package com.distai.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File

class SplitPlanTest {
    private val GB = 1024L

    /** Qwen2.5-7B Q6_K approssimato: 28 layer da ~190 MB, ~900 MB tra embedding e output */
    private val q7b = Gguf.Info("qwen2", 28, 6_254_199_488, LongArray(28) { 190L shl 20 }, 900L shl 20, 4 * 256)

    @Test fun modelloTroppoGrandePerUnTelefono() {
        val p = SplitPlan.make(q7b, 2048, 5 * GB, emptyList())
        assertTrue(p is SplitPlan.TooBig)
    }

    @Test fun dueTelefoniBastanoESommaLayerCorretta() {
        val p = SplitPlan.make(q7b, 2048, 5 * GB, listOf(Node("192.168.1.59", freeMb = 4 * GB)))
        assertTrue(p.toString(), p is SplitPlan.Ok)
        p as SplitPlan.Ok
        assertEquals(28, p.localLayers + p.remoteSplit.sum())
        p.needMb.zip(p.budgetMb).forEach { (need, budget) -> assertTrue("$need > $budget", need <= budget) }
    }

    @Test fun nodoSenzaSpazioEscluso() {
        val p = SplitPlan.make(q7b, 2048, 9 * GB, listOf(Node("10.0.0.2", freeMb = 500)))
        p as SplitPlan.Ok
        assertEquals(28, p.localLayers)
        assertTrue(p.remote.isEmpty())
    }

    // ---- esclusione dei nodi lenti (velocità da RpcBench: gbps e round-trip)

    @Test fun nodoLentoEsclusoSeLaRamBasta() {
        val lento = Node("192.168.1.60", name = "lento", freeMb = 4 * GB, gbps = 3.0, rttMs = 8.0)
        val p = SplitPlan.make(q7b, 2048, 9 * GB, listOf(lento), localGbps = 20.0) as SplitPlan.Ok
        assertTrue("il nodo lento non deve ricevere layer: ${p.describe()}", p.remote.isEmpty())
        assertEquals(28, p.localLayers)
        assertTrue(p.notes.toString(), p.notes.single().startsWith("lento: escluso"))
        assertTrue("costo oltre il 30%: ${p.notes}", "⚠" in p.notes.single())
    }

    @Test fun nodoLentoTenutoSeServeLaSuaRam() {
        val lento = Node("192.168.1.60", name = "lento", freeMb = 4 * GB, gbps = 3.0, rttMs = 8.0)
        val p = SplitPlan.make(q7b, 2048, 5 * GB, listOf(lento), localGbps = 20.0) as SplitPlan.Ok
        assertEquals(listOf("lento"), p.remote.map { it.name })
        assertEquals(28, p.localLayers + p.remoteSplit.sum())
        assertTrue(p.notes.toString(), p.notes.single() == "lento: necessario per la RAM")
    }

    @Test fun nodoVeloceConvieneEPrendeLayer() {
        val veloce = Node("192.168.1.61", name = "veloce", freeMb = 6 * GB, gbps = 40.0, rttMs = 1.5)
        val p = SplitPlan.make(q7b, 2048, 9 * GB, listOf(veloce), localGbps = 10.0) as SplitPlan.Ok
        assertEquals(listOf("veloce"), p.remote.map { it.name })
        assertTrue(p.notes.toString(), p.notes.single().startsWith("veloce: conviene"))
    }

    @Test fun traDueNodiTieneSoloQuelloCheServe() {
        // il coordinatore non basta da solo: serve un nodo, e conviene il più veloce; l'altro resta fuori
        val lento = Node("192.168.1.60", name = "lento", freeMb = 4 * GB, gbps = 3.0, rttMs = 8.0)
        val veloce = Node("192.168.1.61", name = "veloce", freeMb = 4 * GB, gbps = 25.0, rttMs = 2.0)
        val p = SplitPlan.make(q7b, 2048, 5 * GB, listOf(lento, veloce), localGbps = 20.0) as SplitPlan.Ok
        assertEquals(listOf("veloce"), p.remote.map { it.name })
        assertTrue(p.notes.toString(), p.notes.any { it.startsWith("lento: escluso") })
    }

    @Test fun coordinatoreSenzaRamPerEmbeddingEScartato() {
        // 852 MB di embedding+output non stanno in 1 GB liberi meno i margini, anche se i worker hanno RAM per i layer
        val worker = Node("192.168.1.60", name = "pi", freeMb = 8 * GB, gbps = 6.0, rttMs = 2.0)
        val p = SplitPlan.make(q7b, 2048, 1 * GB, listOf(worker), localGbps = 17.0)
        assertTrue(p.toString(), p is SplitPlan.TooBig)
    }

    @Test fun telefonoCoordinatoreUsaSoloIl60PerCento() {
        val pc = Node("192.168.1.30", name = "pc", freeMb = 8 * GB, gbps = 17.0, rttMs = 2.0)
        val pieno = SplitPlan.make(q7b, 2048, 4 * GB, listOf(pc), localGbps = 18.0) as SplitPlan.Ok
        val telefono = SplitPlan.make(q7b, 2048, 4 * GB, listOf(pc), localGbps = 18.0, localAndroid = true) as SplitPlan.Ok
        assertTrue("${telefono.localLayers} < ${pieno.localLayers}", telefono.localLayers < pieno.localLayers)
        assertTrue(telefono.needMb[0] <= 4 * GB * 6 / 10)
    }

    /** Parser sul file reale, se disponibile: -Dgguf=/percorso/modello.gguf */
    @Test fun parserGgufReale() {
        val path = System.getProperty("gguf") ?: System.getenv("GGUF")
        assumeTrue(path != null && File(path).exists())
        val info = Gguf.read(File(path!!))!!
        println("arch=${info.arch} layers=${info.layers} avgLayer=${info.avgLayerBytes shr 20}MB other=${info.otherBytes shr 20}MB kvDim=${info.kvDimPerLayer}")
        assertTrue(info.layers > 0)
        val total = info.layerBytes.sum() + info.otherBytes
        assertTrue("somma tensori $total vs file ${info.sizeBytes}", total in (info.sizeBytes * 9 / 10)..info.sizeBytes)
    }
}
